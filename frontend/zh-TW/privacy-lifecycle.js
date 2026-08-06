(function attachPrivacyLifecycle(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.PrivacyLifecycle = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildPrivacyLifecycle() {
  "use strict";

  function createSensitiveDataLifecycle(options = {}) {
    const revokeObjectUrl =
      typeof options.revokeObjectUrl === "function"
        ? options.revokeObjectUrl
        : (url) => URL.revokeObjectURL(url);

    let canonicalDocument = null;
    let activeRequest = null;
    let requestGeneration = 0;
    const objectUrls = new Set();
    const sectionNodes = new Set();

    function setCanonicalDocument(documentModel) {
      if (
        !documentModel ||
        typeof documentModel !== "object" ||
        Array.isArray(documentModel)
      ) {
        throw new Error("缺少可追溯的 canonical document。");
      }
      canonicalDocument = documentModel;
    }

    function requireCanonicalDocument() {
      if (!canonicalDocument) {
        throw new Error("敏感計算資料已清除或尚未完成計算。");
      }
      return canonicalDocument;
    }

    function registerSectionNode(node) {
      if (!node || typeof node !== "object" || Array.isArray(node)) {
        throw new Error("無法登記輸出區段的敏感資料引用。");
      }
      sectionNodes.add(node);
    }

    function registerObjectUrl(url) {
      if (typeof url !== "string" || !url) {
        throw new Error("無法登記空白的 Blob URL。");
      }
      objectUrls.add(url);
      return url;
    }

    function releaseObjectUrl(url) {
      if (!objectUrls.delete(url)) return false;
      try {
        revokeObjectUrl(url);
      } catch (_error) {
        // Revoke failure must not restore the URL to the active registry or
        // expose exception details. Browser/OS retention remains out of scope.
      }
      return true;
    }

    function beginRequest(controller) {
      if (!controller || typeof controller.abort !== "function") {
        throw new Error("敏感 request 必須使用可中止的 AbortController。");
      }
      if (activeRequest) {
        try {
          activeRequest.controller.abort();
        } catch (_error) {
          // A failed abort still invalidates the old generation below.
        }
      }
      requestGeneration += 1;
      const token = Object.freeze({ generation: requestGeneration });
      activeRequest = { controller, token };
      return token;
    }

    function isCurrentRequest(token) {
      return Boolean(activeRequest && activeRequest.token === token);
    }

    function finishRequest(token) {
      if (!isCurrentRequest(token)) return false;
      activeRequest = null;
      return true;
    }

    /**
     * 中止進行中的請求，但**不動**已算出的結果。
     *
     * `clear()` 也會 abort，可是它同時作廢 canonical document、section 參照
     * 與 Blob URL——那是「清除」的語意。使用者按「中止這次計算」時要的是
     * 停下這一次，不是把上一次的結果一起收掉（PIA-2026-08-06-007）。
     *
     * 回傳是否真的有一個請求被中止，讓呼叫端不必猜。
     */
    function abortActiveRequest() {
      if (!activeRequest) return false;
      // generation 先失效：即使 abort() 丟例外，遲到的回應也必須被擋下。
      requestGeneration += 1;
      try {
        activeRequest.controller.abort();
      } catch (_error) {
        // 已作廢的 generation 就是保護本身。
      }
      activeRequest = null;
      return true;
    }

    function clear() {
      const receipt = {
        canonical_document_cleared: canonicalDocument !== null,
        object_urls_revoked: objectUrls.size,
        section_references_cleared: sectionNodes.size,
        request_aborted: activeRequest !== null,
      };

      canonicalDocument = null;
      requestGeneration += 1;

      if (activeRequest) {
        try {
          activeRequest.controller.abort();
        } catch (_error) {
          // The request generation is already invalid even if abort() fails.
        }
        activeRequest = null;
      }

      for (const node of sectionNodes) {
        try {
          node._canonicalExportSection = null;
        } catch (_error) {
          // Continue clearing every other registered reference.
        }
      }
      sectionNodes.clear();

      for (const url of Array.from(objectUrls)) {
        releaseObjectUrl(url);
      }

      return receipt;
    }

    function inspect() {
      return {
        has_canonical_document: canonicalDocument !== null,
        active_object_url_count: objectUrls.size,
        registered_section_count: sectionNodes.size,
        request_active: activeRequest !== null,
      };
    }

    return Object.freeze({
      setCanonicalDocument,
      requireCanonicalDocument,
      registerSectionNode,
      registerObjectUrl,
      releaseObjectUrl,
      beginRequest,
      isCurrentRequest,
      finishRequest,
      abortActiveRequest,
      clear,
      inspect,
    });
  }

  return Object.freeze({ createSensitiveDataLifecycle });
});
