const test = require("node:test");
const assert = require("node:assert/strict");

const PrivacyLifecycle = require("../zh-TW/privacy-lifecycle.js");

test("canonical document rejects arrays and preserves a valid object", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });

  assert.throws(
    () => lifecycle.setCanonicalDocument([]),
    /canonical document/
  );
  const documentModel = {
    calculation_dossier: { dossier_version: "0.3.0" },
    sections: [],
  };
  lifecycle.setCanonicalDocument(documentModel);
  assert.equal(lifecycle.requireCanonicalDocument(), documentModel);
});

test("section reference registry rejects arrays", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });

  assert.throws(
    () => lifecycle.registerSectionNode([]),
    /輸出區段/
  );
});

test("clear drops the canonical document, section references, and Blob URLs", () => {
  const revoked = [];
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: (url) => revoked.push(url),
  });
  const documentModel = {
    calculation_dossier: {
      input_receipt: {
        datetime: { year: 1997 },
        location: { latitude: 24.1477, longitude: 120.6736 },
      },
    },
  };
  const sectionNode = { _canonicalExportSection: { rows: [["1997"]] } };

  lifecycle.setCanonicalDocument(documentModel);
  lifecycle.registerSectionNode(sectionNode);
  lifecycle.registerObjectUrl("blob:birth-data-one");
  lifecycle.registerObjectUrl("blob:birth-data-two");

  assert.equal(lifecycle.requireCanonicalDocument(), documentModel);
  assert.deepEqual(lifecycle.inspect(), {
    has_canonical_document: true,
    active_object_url_count: 2,
    registered_section_count: 1,
    request_active: false,
  });

  assert.deepEqual(lifecycle.clear(), {
    canonical_document_cleared: true,
    object_urls_revoked: 2,
    section_references_cleared: 1,
    request_aborted: false,
  });
  assert.equal(sectionNode._canonicalExportSection, null);
  assert.deepEqual(revoked, [
    "blob:birth-data-one",
    "blob:birth-data-two",
  ]);
  assert.throws(
    () => lifecycle.requireCanonicalDocument(),
    /已清除或尚未完成計算/
  );
  assert.deepEqual(lifecycle.inspect(), {
    has_canonical_document: false,
    active_object_url_count: 0,
    registered_section_count: 0,
    request_active: false,
  });
});

test("clear aborts the request and invalidates a late sensitive response", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });
  const controller = new AbortController();
  const requestToken = lifecycle.beginRequest(controller);

  assert.equal(lifecycle.isCurrentRequest(requestToken), true);
  const receipt = lifecycle.clear();

  assert.equal(receipt.request_aborted, true);
  assert.equal(controller.signal.aborted, true);
  assert.equal(lifecycle.isCurrentRequest(requestToken), false);
});

test("a newer request invalidates and aborts the previous request", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });
  const firstController = new AbortController();
  const firstToken = lifecycle.beginRequest(firstController);
  const secondController = new AbortController();
  const secondToken = lifecycle.beginRequest(secondController);

  assert.equal(firstController.signal.aborted, true);
  assert.equal(lifecycle.isCurrentRequest(firstToken), false);
  assert.equal(lifecycle.isCurrentRequest(secondToken), true);
  lifecycle.finishRequest(secondToken);
  assert.equal(lifecycle.inspect().request_active, false);
});

test("releasing a Blob URL is idempotent and clear does not revoke it twice", () => {
  const revoked = [];
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: (url) => revoked.push(url),
  });

  lifecycle.registerObjectUrl("blob:short-lived");
  assert.equal(lifecycle.releaseObjectUrl("blob:short-lived"), true);
  assert.equal(lifecycle.releaseObjectUrl("blob:short-lived"), false);
  lifecycle.clear();

  assert.deepEqual(revoked, ["blob:short-lived"]);
});

// ── PIA-2026-08-06-007 ───────────────────────────────────────
// 送出後唯一會 abort 的入口是兩個清除鈕，而它們在第一次成功前不可達。
// 新增的中止入口必須只停這一次請求，不得順手把已算出的結果一起收掉。

test("aborting the active request stops it without clearing results", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });
  lifecycle.setCanonicalDocument({ export_contract_version: "0.1.2" });
  const controller = new AbortController();
  const token = lifecycle.beginRequest(controller);

  assert.equal(lifecycle.abortActiveRequest(), true);
  assert.equal(controller.signal.aborted, true);
  assert.equal(lifecycle.isCurrentRequest(token), false, "遲到的回應仍被視為當前");
  assert.equal(lifecycle.inspect().request_active, false);
  // 這一項是與 clear() 的分界：中止不是清除。
  assert.deepEqual(lifecycle.requireCanonicalDocument(),
    { export_contract_version: "0.1.2" });
});

test("aborting when nothing is in flight reports that honestly", () => {
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });
  assert.equal(lifecycle.abortActiveRequest(), false);
});

test("a late response cannot revive itself after an abort", () => {
  // generation 必須在 abort() 之前就失效，否則 abort() 丟例外時
  // 遲到的回應會被當成當前結果填回畫面。
  const lifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: () => {},
  });
  const hostile = {
    signal: {},
    abort() { throw new Error("abort refused"); },
  };
  const token = lifecycle.beginRequest(hostile);
  assert.equal(lifecycle.abortActiveRequest(), true);
  assert.equal(lifecycle.isCurrentRequest(token), false);
});
