/* 时钟校准修订：预览影响窗口、确认修订、渲染差异表与复核导出链接 */
(function () {
  "use strict";

  function el(id) {
    return document.getElementById(id);
  }

  function setHint(text, kind) {
    const node = el("revision-hint");
    if (!node) {
      return;
    }
    node.textContent = text;
    node.className = "hint" + (kind ? " " + kind : "");
  }

  function cell(text, mono) {
    const td = document.createElement("td");
    td.tabIndex = 0;
    if (mono) {
      td.className = "mono";
    }
    td.textContent = text;
    return td;
  }

  function numCell(text) {
    const td = cell(String(text), false);
    td.className = "num";
    return td;
  }

  function tagCell(text, cls) {
    const td = document.createElement("td");
    td.tabIndex = 0;
    const span = document.createElement("span");
    span.className = "tag " + cls;
    span.textContent = text;
    td.appendChild(span);
    return td;
  }

  function clearBody(tableId) {
    const table = el(tableId);
    table.querySelector("tbody").textContent = "";
    return table;
  }

  function renderSummary(payload) {
    const impact = payload.impact || {};
    const rows = [
      ["修订号", payload.revision.revision_id],
      ["状态", payload.revision.status],
      ["迁移采样（桶变化）", impact.moved_samples],
      ["其中跨过 15 分钟边界", impact.crossed_window_samples],
      ["受影响窗口", impact.affected_windows],
      ["覆盖状态翻转窗口", impact.state_changed_windows],
    ];
    if (payload.run) {
      rows.push(["更正批次号", payload.run.run_id]);
      rows.push(["更正批次四账", payload.run.balanced ? "平" : "不平"]);
    }
    const table = clearBody("revision-summary-table");
    const tbody = table.querySelector("tbody");
    rows.forEach(function (item, index) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.className = "gutter";
      th.textContent = String(index + 1);
      tr.appendChild(th);
      tr.appendChild(cell(item[0]));
      tr.appendChild(cell(String(item[1])));
      tbody.appendChild(tr);
    });
  }

  function renderWindows(payload) {
    const table = clearBody("revision-windows-table");
    const tbody = table.querySelector("tbody");
    (payload.window_changes || []).forEach(function (row, index) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.className = "gutter";
      th.textContent = String(index + 1);
      tr.appendChild(th);
      tr.appendChild(cell(row.sensor_id, true));
      tr.appendChild(cell(row.window_start, true));
      tr.appendChild(numCell(row.old_observed + " → " + row.new_observed));
      tr.appendChild(numCell(row.old_coverage + " → " + row.new_coverage));
      tr.appendChild(numCell(row.old_state + " → " + row.new_state));
      tr.appendChild(tagCell(row.state_changed ? "状态翻转" : "覆盖变化", row.state_changed ? "bad" : "warn"));
      tbody.appendChild(tr);
    });
  }

  function renderMoved(payload) {
    const table = clearBody("revision-moved-table");
    const tbody = table.querySelector("tbody");
    (payload.moved || []).forEach(function (row, index) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.className = "gutter";
      th.textContent = String(index + 1);
      tr.appendChild(th);
      tr.appendChild(numCell(row.source_row));
      tr.appendChild(cell(row.sensor_id, true));
      tr.appendChild(cell(row.old_bucket + " → " + row.new_bucket, true));
      tr.appendChild(cell(row.old_window + " → " + row.new_window, true));
      tr.appendChild(numCell(row.old_offset + " → " + row.new_offset));
      tr.appendChild(tagCell(row.crossed_window ? "跨窗" : "桶内", row.crossed_window ? "bad" : "quiet"));
      tbody.appendChild(tr);
    });
  }

  function wireExportLinks(revisionId) {
    el("revision-windows-csv").href = "/api/revisions/" + revisionId + "/windows.csv";
    el("revision-moved-csv").href = "/api/revisions/" + revisionId + "/moved.csv";
  }

  function render(payload) {
    el("revision-result").hidden = false;
    renderSummary(payload);
    renderWindows(payload);
    renderMoved(payload);
    wireExportLinks(payload.revision.revision_id);
  }

  function bodyOf(form) {
    return {
      run_id: el("revision-run").value,
      offsets: el("revision-offsets").value
    };
  }

  async function post(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const json = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error(json.message || json.error || ("HTTP " + response.status));
    }
    return json;
  }

  function bind() {
    const form = el("revision-form");
    if (!form) {
      return;
    }
    const previewBtn = el("revision-preview-btn");
    const applyBtn = el("revision-apply-btn");

    previewBtn.addEventListener("click", async function () {
      previewBtn.disabled = true;
      setHint("重算影响窗口中…", "");
      try {
        const payload = await post("/api/revisions/preview", bodyOf(form));
        render(payload);
        setHint((payload.reused ? "复用既有预览 · " : "预览完成 · ") + payload.revision.revision_id, "ok");
      } catch (error) {
        setHint("预览失败（旧版结果保留）：" + error.message, "bad");
      } finally {
        previewBtn.disabled = false;
      }
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (applyBtn.disabled) {
        return;
      }
      applyBtn.disabled = true;
      setHint("落更正批次中…", "");
      try {
        const payload = await post("/api/revisions/apply", bodyOf(form));
        render(payload);
        setHint(
          (payload.reused ? "修订已存在，复用 · " : "修订已生效 · ") +
            payload.revision.revision_id +
            " → " +
            payload.run.run_id,
          "ok"
        );
        window.setTimeout(function () {
          window.location.reload();
        }, 900);
      } catch (error) {
        setHint("修订失败，正式结果保留旧版：" + error.message, "bad");
      } finally {
        applyBtn.disabled = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", bind);
})();
