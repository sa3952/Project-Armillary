"""收集「計算過程」步驟，供前端逐步顯示以供驗證。"""

from dataclasses import dataclass, field


@dataclass
class Trace:
    steps: list = field(default_factory=list)

    def add(self, title: str, formula: str = "", inputs: dict | None = None, result: dict | None = None, note: str = ""):
        self.steps.append(
            {
                "title": title,
                "formula": formula,
                "inputs": inputs or {},
                "result": result or {},
                "note": note,
            }
        )

    def as_list(self):
        return self.steps
