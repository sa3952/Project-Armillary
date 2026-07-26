"""Low-risk, static accessibility contracts for native frontend controls."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"
APP_JS = PROJECT_ROOT / "frontend" / "app.js"


class _FormControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._open_labels = 0
        self.controls: dict[str, dict[str, str | None]] = {}
        self.nested_label_ids: set[str] = set()
        self.explicit_label_targets: set[str] = set()
        self.ids: list[str] = []
        self.text_by_id: dict[str, list[str]] = {}
        self._element_stack: list[tuple[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)
            self.text_by_id.setdefault(element_id, [])
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self._element_stack.append((tag, element_id or ""))
        if tag == "label":
            self._open_labels += 1
            target = attributes.get("for")
            if target:
                self.explicit_label_targets.add(target)
        if tag in {"input", "select", "textarea"} and element_id:
            self.controls[element_id] = attributes
            if self._open_labels:
                self.nested_label_ids.add(element_id)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._open_labels -= 1
        if self._element_stack and self._element_stack[-1][0] == tag:
            self._element_stack.pop()

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for _tag, element_id in self._element_stack:
            if element_id:
                self.text_by_id[element_id].append(data)

    def aria_labelledby_has_text(self, value: str | None) -> bool:
        if not value:
            return False
        references = value.split()
        return bool(references) and all(
            reference in self.text_by_id
            and bool("".join(self.text_by_id[reference]).strip())
            for reference in references
        )


def test_every_identified_native_form_control_has_an_accessible_name():
    parser = _FormControlParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    unnamed: list[str] = []
    for element_id, attributes in parser.controls.items():
        if attributes.get("type") in {"hidden", "radio", "checkbox"}:
            # Radios and checkboxes are text-nested in labels in the current UI.
            assert element_id in parser.nested_label_ids
            continue
        has_name = (
            element_id in parser.nested_label_ids
            or element_id in parser.explicit_label_targets
            or bool(attributes.get("aria-label"))
            or parser.aria_labelledby_has_text(attributes.get("aria-labelledby"))
        )
        if not has_name:
            unnamed.append(element_id)

    assert unnamed == [], f"native controls without accessible names: {unnamed}"


def test_aria_labelledby_requires_existing_nonempty_text_target():
    parser = _FormControlParser()
    parser.feed(
        '<span id="good-name">Visible name</span>'
        '<span id="empty-name"></span>'
        '<input id="good" aria-labelledby="good-name">'
        '<input id="multiple-good" aria-labelledby="good-name second-name">'
        '<input id="missing" aria-labelledby="missing-name">'
        '<input id="multiple-missing" aria-labelledby="good-name missing-name">'
        '<input id="empty" aria-labelledby="empty-name">'
        '<span id="second-name">Second visible name</span>'
    )

    assert parser.aria_labelledby_has_text("good-name") is True
    assert parser.aria_labelledby_has_text("good-name second-name") is True
    assert parser.aria_labelledby_has_text("missing-name") is False
    assert parser.aria_labelledby_has_text("good-name missing-name") is False
    assert parser.aria_labelledby_has_text("empty-name") is False


def test_dom_ids_are_unique():
    parser = _FormControlParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    duplicates = sorted(
        element_id for element_id in set(parser.ids) if parser.ids.count(element_id) > 1
    )
    assert duplicates == []


def test_generated_table_headers_are_explicit_column_headers():
    script = APP_JS.read_text(encoding="utf-8")

    assert 'el("th", { scope: "col", text: h })' in script
