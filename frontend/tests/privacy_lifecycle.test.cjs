const test = require("node:test");
const assert = require("node:assert/strict");

const PrivacyLifecycle = require("../privacy-lifecycle.js");

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
