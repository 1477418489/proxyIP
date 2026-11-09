"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 50;
  const MAX_JOB_CANDIDATES = 500;
  const state = {
    token: "",
    needsAuth: false,
    mode: "local",
    country: "all",
    countryBasis: "candidate",
    status: null,
    items: [],
    sources: [],
    jobs: [],
    selected: new Set(),
    page: 1,
    loaded: false,
    refreshing: false,
    refreshAgain: false,
    busyImport: false,
    busySource: false,
    busyJob: false,
    busyCancel: false,
    busyGeo: false,
    geoActionError: "",
    sourceFetches: new Set(),
    deleting: new Set(),
    historyRequest: 0,
  };
  let pollTimer;
  let regionNames;
  try {
    regionNames = new Intl.DisplayNames(["zh-CN"], { type: "region" });
  } catch {
    regionNames = null;
  }

  class AuthRequired extends Error {}

  function element(tag, className = "", text = null) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = String(text);
    return node;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function number(value, digits = 0) {
    return finite(value)
      ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits })
      : "—";
  }

  function milliseconds(value) {
    return finite(value) ? number(value, value < 10 ? 1 : 0) : "—";
  }

  function percentage(value) {
    return finite(value) ? `${number(value * 100, 1)}%` : "—";
  }

  function dateText(value, compact = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "—";
    return date.toLocaleString(
      "zh-CN",
      compact
        ? {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }
        : {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          },
    );
  }

  function modeName(mode) {
    return mode === "worker" ? "Workers 路径" : "本地检测";
  }

  function countryCode(value) {
    const code = typeof value === "string" ? value.toUpperCase() : "";
    return /^[A-Z]{2}$/.test(code) && !["XX", "ZZ"].includes(code)
      ? code
      : null;
  }

  function countryLabel(value, preferredName = "") {
    const code = countryCode(value);
    if (!code) return "未知/未分类";
    const name =
      preferredName && /[\u3400-\u9fff]/.test(preferredName)
        ? preferredName
        : regionNames?.of(code) || preferredName || code;
    return name === code ? code : `${name} ${code}`;
  }

  function exitCountries(summary) {
    return [
      ...new Set(
        (summary?.exit_countries || []).map(countryCode).filter(Boolean),
      ),
    ];
  }

  function matchesCountry(item) {
    if (state.country === "all") return true;
    const codes =
      state.countryBasis === "candidate"
        ? [countryCode(item.country?.code)].filter(Boolean)
        : exitCountries(item.latest);
    return state.country === "unknown"
      ? codes.length === 0
      : codes.includes(state.country);
  }

  function countrySearchText(item) {
    const exits = exitCountries(item.latest);
    return [
      countryLabel(item.country?.code, item.country?.name),
      ...(exits.length
        ? exits.map((code) => countryLabel(code))
        : ["未知 未分类"]),
    ].join(" ");
  }

  function geoUpdating() {
    return state.busyGeo || Boolean(state.status?.geo?.updating);
  }

  function renderGeo() {
    const geo = state.status?.geo;
    const needsRestart = Boolean(state.status && !geo);
    const updating = geoUpdating();
    $("geo-update").disabled =
      !state.status ||
      needsRestart ||
      state.needsAuth ||
      updating ||
      state.sourceFetches.size > 0;
    $("geo-update").textContent = updating
      ? "地区库下载更新中…"
      : geo?.ready
        ? "更新地区库"
        : "下载地区库";
    $("geo-status").textContent = updating
      ? "正在下载并校验地区库"
      : geo?.ready
        ? `DB-IP Country Lite · ${geo.version || "当前版本"}`
        : "尚未加载地区库";
    $("geo-description").textContent = updating
      ? "下载更新最多约 60 秒；已有有效地区库继续用于当前展示。"
      : geo?.ready
        ? `数据库构建时间：${dateText(geo.build_date)}。候选归属由离线数据库识别，已有候选无需重新导入。`
        : "候选归属显示为未知/未分类，不代表某个地区。下载地区库后自动补充已有候选。";
    const proxy = $("fetch-use-proxy").checked;
    $("geo-action-hint").textContent =
      `下载地区库使用“来源与导入”中的代理设置：${proxy ? "HTTP 代理" : "直连"}；首次下载约 4 MiB，不会自动下载。`;
    const error = state.geoActionError || geo?.error;
    $("geo-error").hidden = !error;
    $("geo-error").textContent = error || "";
    $("geo-panel").setAttribute("aria-busy", String(updating));
    if (needsRestart) {
      $("geo-update").textContent = "请先重启后端";
      $("geo-status").textContent = "后端版本较旧，需要重启服务";
      $("geo-description").textContent =
        "页面已更新，但当前后端没有地区库接口。请停止旧服务后重新启动，再刷新页面；已有候选和历史会保留。";
      $("geo-action-hint").textContent =
        "Python：重新运行 python -m cfproxy。Docker：运行 docker compose up -d --build。仅刷新浏览器不会加载新的后端代码。";
      $("geo-error").hidden = true;
    }
  }

  function renderCountryFilters() {
    const counts = new Map();
    const names = new Map();
    let unknown = 0;
    for (const item of state.items) {
      const code = countryCode(item.country?.code);
      if (code && item.country?.name) names.set(code, item.country.name);
      const codes =
        state.countryBasis === "candidate"
          ? [code].filter(Boolean)
          : exitCountries(item.latest);
      if (!codes.length) unknown += 1;
      for (const known of codes)
        counts.set(known, (counts.get(known) || 0) + 1);
    }
    if (
      !["all", "unknown"].includes(state.country) &&
      !counts.has(state.country)
    )
      counts.set(state.country, 0);
    const fragment = document.createDocumentFragment();
    const option = (value, label) => {
      const node = element("option", "", label);
      node.value = value;
      fragment.append(node);
    };
    option("all", `全部国家/地区 · ${number(state.items.length)}`);
    option("unknown", `未知/未分类 · ${number(unknown)}`);
    const sorted = [...counts].sort(([a], [b]) =>
      countryLabel(a, names.get(a)).localeCompare(
        countryLabel(b, names.get(b)),
        "zh-CN",
      ),
    );
    for (const [code, count] of sorted)
      option(code, `${countryLabel(code, names.get(code))} · ${number(count)}`);
    $("country-filter").replaceChildren(fragment);
    $("country-filter").value = state.country;
    $("country-basis").value = state.countryBasis;
    $("country-filter-note").textContent =
      state.countryBasis === "exit"
        ? "匹配某地区表示最新检测至少一次成功样本观察到该地区；多地区出口可能同时匹配多个筛选。"
        : "候选归属来自 IP 地理数据库，不代表物理位置或实际出口。";
    const region =
      state.country === "all"
        ? "全部地区"
        : state.country === "unknown"
          ? "未知/未分类"
          : countryLabel(state.country, names.get(state.country));
    $("export-scope-note").textContent =
      `导出范围：${modeName(state.mode)} · ${state.countryBasis === "candidate" ? "候选归属" : "观测出口"} · ${region}。仅导出最新完整通过且在有效期内的结果；不受搜索关键词、状态筛选或勾选影响，取消或中断不沿用旧成功。`;
  }

  function statusName(status, mode = state.mode) {
    return (
      {
        passed: mode === "worker" ? "Workers 路径通过" : "本地通过",
        unstable: "不稳定",
        failed: "失败",
        queued: "等待检测",
        running: "检测中",
        cancelled: "已取消",
        interrupted: "已中断",
        untested: "未检测",
        completed: "已完成",
      }[status] || "未知状态"
    );
  }

  function badge(status, mode = state.mode) {
    const known = [
      "passed",
      "unstable",
      "failed",
      "queued",
      "running",
      "cancelled",
      "interrupted",
      "untested",
    ];
    return element(
      "span",
      `badge ${known.includes(status) ? status : "neutral"}`,
      statusName(status, mode),
    );
  }

  function errorMessage(error) {
    return error instanceof Error
      ? error.message
      : String(error || "操作失败，请重试");
  }

  function apiDetail(payload, fallback) {
    const detail = payload?.detail ?? payload?.error;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail))
      return detail
        .map((item) => {
          const location = Array.isArray(item.loc)
            ? item.loc.filter((part) => part !== "body").join(".")
            : "";
          return `${location ? `${location}：` : ""}${item.msg || "参数不正确"}`;
        })
        .join("；");
    return fallback;
  }

  function toast(message, kind = "") {
    const node = element("div", `toast ${kind}`, message);
    $("toast-stack").append(node);
    while ($("toast-stack").children.length > 4)
      $("toast-stack").firstElementChild.remove();
    setTimeout(() => node.remove(), kind === "error" ? 8500 : 5000);
  }

  function showAuth(required = false) {
    $("auth-error").hidden = true;
    $("auth-description").textContent = required
      ? "服务需要管理 token。输入后可访问候选和检测结果，密钥只保存在当前页面内存中。"
      : "如果服务启用了管理 token，请在此输入。密钥只保存在当前页面内存中。";
    $("auth-clear").disabled = !state.token;
    if (!$("auth-dialog").open) $("auth-dialog").showModal();
    $("auth-token").focus();
  }

  async function request(path, options = {}) {
    const { body, raw = false, authPrompt = true, ...rest } = options;
    const headers = { Accept: raw ? "*/*" : "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      ...rest,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (response.status === 401) {
      state.needsAuth = true;
      setConnection(false, "需要管理密钥");
      if (authPrompt) showAuth(true);
      throw new AuthRequired("管理 token 缺失或无效");
    }
    if (!response.ok) {
      let payload;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      throw new Error(
        apiDetail(payload, `请求失败（HTTP ${response.status}）`),
      );
    }
    if (raw) return response;
    if (response.status === 204) return null;
    return response.json();
  }

  function handleError(error) {
    if (!(error instanceof AuthRequired)) toast(errorMessage(error), "error");
  }

  function setConnection(connected, message = "") {
    $("sidebar-dot").className =
      `connection-dot ${connected ? "connected" : "error"}`;
    $("sidebar-status").textContent = connected
      ? "服务已连接"
      : message || "服务未连接";
    $("service-label").textContent = connected
      ? "● 服务已连接"
      : message || "服务未连接";
    $("service-label").classList.toggle("connected", connected);
  }

  function activeJob() {
    const job = state.status?.active_job;
    return job && job.state === "running" ? job : null;
  }

  function renderOverview() {
    const count = state.status?.counts?.candidates;
    $("stat-candidates").textContent = number(count);
    $("stat-source-note").textContent = state.status
      ? `${number(state.status.counts?.sources ?? state.sources.length)} 个来源 · 候选上限 ${number(state.status.limits?.max_candidates)}`
      : "等待加载候选池";
    $("stat-passed-label").textContent =
      state.mode === "worker" ? "Workers 路径通过" : "本地检测通过";
    $("stat-passed").textContent = state.loaded
      ? number(
          state.items.filter((item) => item.latest?.status === "passed").length,
        )
      : "—";
    $("stat-recheck").textContent = state.loaded
      ? number(
          state.items.filter((item) =>
            ["failed", "unstable"].includes(item.latest?.status),
          ).length,
        )
      : "—";
    const job = activeJob();
    $("stat-task").textContent = state.status ? (job ? "检测中" : "空闲") : "—";
    $("stat-task-note").textContent = job
      ? `${modeName(job.mode)} · ${number(job.completed)} / ${number(job.total)}`
      : "可开始一项新的检测";
    $("import-limit").textContent = state.status?.limits?.max_import
      ? `每次最多 ${number(state.status.limits.max_import)} 个地址`
      : "手动导入 / 文件 / 远程来源";
  }

  function renderMode() {
    const configured = Boolean(state.status?.worker_configured);
    $("mode-local").classList.toggle("selected", state.mode === "local");
    $("mode-local").setAttribute(
      "aria-pressed",
      String(state.mode === "local"),
    );
    $("mode-worker").classList.toggle("selected", state.mode === "worker");
    $("mode-worker").setAttribute(
      "aria-pressed",
      String(state.mode === "worker"),
    );
    $("mode-worker").disabled = !configured;
    $("mode-worker").title = configured
      ? "通过已配置的 Worker 验证候选路径"
      : "按 README 配置并部署 Worker 探针后启用";
    $("worker-mode-label").hidden = configured;
    $("mode-description").textContent =
      state.mode === "worker"
        ? "通过自己的 Worker 连接候选，验证目标的 TLS / HTTP；结果仅代表所测 Workers 路径。"
        : "本地通过仅代表当前机器到候选的 TLS / HTTP 路径，Workers 尚未验证。";
    $("task-mode-badge").textContent =
      state.mode === "worker" ? "Workers 模式" : "本地模式";
    $("path-start").textContent =
      state.mode === "worker" ? "我的 Worker" : "当前机器";
    $("worker-note").hidden = configured;
  }

  function renderTargets() {
    const targets = state.status?.targets || [];
    const fragment = document.createDocumentFragment();
    for (const target of targets) {
      const name =
        typeof target === "string"
          ? target
          : target.host ||
            target.hostname ||
            target.name ||
            target.url ||
            "检测目标";
      fragment.append(element("span", "target-pill", name));
    }
    if (targets.length) {
      const samples = Number($("job-samples").value);
      if (Number.isInteger(samples) && samples >= 1 && samples <= 5)
        fragment.append(
          element(
            "span",
            "target-pill",
            `每地址 ${targets.length * samples} 次`,
          ),
        );
    } else fragment.append(element("span", "muted-label", "等待加载验证目标"));
    $("target-list").replaceChildren(fragment);
  }

  function renderJob() {
    const job = activeJob();
    const totalCandidates = state.status?.counts?.candidates || 0;
    const scopeCount = state.selected.size;
    const scopeTooLarge = scopeCount > MAX_JOB_CANDIDATES;
    $("job-scope").textContent = state.selected.size
      ? `已选择 ${number(state.selected.size)} 个候选，将只检测这些地址`
      : "请先在候选池中选择要检测的地址";
    $("job-scope").classList.toggle("danger-text", scopeTooLarge);
    $("clear-selection").disabled = !state.selected.size;
    $("job-submit").disabled =
      Boolean(job) ||
      state.busyJob ||
      !state.status ||
      totalCandidates === 0 ||
      scopeCount === 0 ||
      scopeTooLarge ||
      state.needsAuth;
    $("job-submit-label").textContent = state.busyJob
      ? "正在创建任务…"
      : job
        ? "已有任务正在运行"
        : scopeTooLarge
          ? `单次最多检测 ${MAX_JOB_CANDIDATES} 个候选`
          : scopeCount === 0
            ? "请先选择候选"
            : state.mode === "worker"
              ? "开始 Workers 路径检测"
              : "开始本地检测";
    for (const id of ["job-concurrency", "job-samples", "job-timeout"])
      $(id).disabled = Boolean(job) || state.busyJob;
    $("active-job").hidden = !job;
    $("job-cancel").disabled = state.busyCancel;
    $("job-cancel").textContent = state.busyCancel ? "正在取消…" : "取消任务";
    if (job) {
      $("active-job-title").textContent = `${modeName(job.mode)}进行中`;
      $("job-progress").value =
        job.total > 0 ? Math.min(100, (job.completed / job.total) * 100) : 0;
      $("job-progress-text").textContent =
        `已处理 ${number(job.completed)} / ${number(job.total)} 个候选`;
      $("job-progress-passed").textContent = `${number(job.passed)} 个通过`;
    }
  }

  function filteredItems() {
    const query = $("search-input").value.trim().toLocaleLowerCase();
    const filter = $("status-filter").value;
    const items = state.items.filter((item) => {
      if (!matchesCountry(item)) return false;
      const status = item.latest?.status || "untested";
      if (filter !== "all" && status !== filter) return false;
      return (
        !query ||
        [
          item.endpoint,
          item.host,
          ...(item.sources || []),
          countrySearchText(item),
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(query)
      );
    });
    const sort = $("sort-filter").value;
    items.sort((left, right) => {
      let difference = 0;
      if (sort === "score")
        difference = (right.latest?.score ?? -1) - (left.latest?.score ?? -1);
      if (sort === "latency")
        difference =
          (left.latest?.p50_ms ?? Infinity) -
          (right.latest?.p50_ms ?? Infinity);
      if (sort === "recent")
        difference =
          (Date.parse(right.latest?.checked_at) || 0) -
          (Date.parse(left.latest?.checked_at) || 0);
      return (
        (Number.isNaN(difference) ? 0 : difference) ||
        String(left.endpoint).localeCompare(String(right.endpoint), "en", {
          numeric: true,
        })
      );
    });
    return items;
  }

  function renderCandidates() {
    const focused = $("candidates-body").contains(document.activeElement)
      ? document.activeElement.id
      : null;
    const items = filteredItems();
    $("selection-summary").textContent = state.selected.size
      ? `已选 ${number(state.selected.size)} 个 · 筛选结果 ${number(items.length)} 个`
      : `尚未选择 · 筛选结果 ${number(items.length)} 个`;
    const canBatchSelect = state.loaded && items.length > 0;
    $("select-first").disabled = !canBatchSelect;
    document.querySelectorAll("[data-select-count]").forEach((button) => {
      button.disabled = !canBatchSelect;
    });
    $("clear-selection").disabled = !state.selected.size;
    const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    state.page = Math.min(state.page, pages);
    const first = (state.page - 1) * PAGE_SIZE;
    const pageItems = items.slice(first, first + PAGE_SIZE);
    const fragment = document.createDocumentFragment();
    for (const item of pageItems) {
      const latest = item.latest;
      const row = element("tr", state.selected.has(item.id) ? "selected" : "");
      const selectCell = element("td", "select-cell");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.id = `candidate-select-${item.id}`;
      checkbox.checked = state.selected.has(item.id);
      checkbox.setAttribute("aria-label", `选择 ${item.endpoint}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(item.id);
        else state.selected.delete(item.id);
        renderCandidates();
        renderJob();
      });
      selectCell.append(checkbox);

      const endpointCell = element("td");
      const endpoint = element("span", "endpoint", item.endpoint);
      endpoint.title = item.endpoint;
      const source = element(
        "span",
        "cell-subtext",
        (item.sources || []).join(" · ") || "手动导入",
      );
      source.title = (item.sources || []).join(" · ");
      endpointCell.append(endpoint, source);

      const countryCell = element("td", "country-cell");
      const candidateCountry = element(
        "span",
        "country-value",
        `归属：${countryLabel(item.country?.code, item.country?.name)}`,
      );
      candidateCountry.title = item.country
        ? `${item.country.source || "DB-IP Country Lite"} ${item.country.version || ""} 的候选 IP 地理识别，不代表物理位置或实际出口。`
        : "未分类；下载地区库后仍可能无法识别部分 IP。";
      countryCell.append(candidateCountry);
      const observed = exitCountries(latest);
      const exitLabel = observed.length
        ? `${observed.length > 1 ? "出口多地区" : "出口"}：${observed.map((code) => countryLabel(code)).join(" / ")}`
        : "出口：未知/未分类";
      const observedCountry = element("span", "country-exit", exitLabel);
      observedCountry.title =
        "来自当前模式最新检测的成功样本；每个地区至少观察到一次，不代表所有采样均从该地区出口，也不是 colo 机房位置。";
      countryCell.append(observedCountry);
      const unknownSamples = latest?.country_unknown_samples || 0;
      if (observed.length)
        countryCell.append(
          element("span", "cell-subtext", "至少一次成功样本观测"),
        );
      if (unknownSamples > 0)
        countryCell.append(
          element(
            "span",
            "cell-subtext",
            `${number(unknownSamples)} 个成功样本地区未知`,
          ),
        );

      const statusCell = element("td");
      statusCell.append(badge(latest?.status || "untested"));
      if (latest?.status === "passed" && state.mode === "local")
        statusCell.append(element("span", "cell-subtext", "Workers 未验证"));
      else if (latest?.worker_colos?.length)
        statusCell.append(
          element(
            "span",
            "cell-subtext",
            `Worker：${latest.worker_colos.join(" / ")}`,
          ),
        );
      if (latest?.last_error) {
        const error = element("span", "cell-subtext error", latest.last_error);
        error.title = latest.last_error;
        statusCell.append(error);
      }

      const rateCell = element("td");
      rateCell.append(
        element(
          "span",
          `rate-value ${latest?.status === "passed" ? "complete" : ""}`,
          latest?.attempts ? percentage(latest.success_rate) : "—",
        ),
      );
      rateCell.append(
        element(
          "span",
          "cell-subtext",
          latest
            ? `${latest.successes || 0} 次通过 · ${latest.attempts || 0}/${latest.expected_samples || 0} 已测`
            : "尚未采样",
        ),
      );

      const latencyCell = element("td");
      latencyCell.append(
        element(
          "span",
          "latency-value",
          latest && finite(latest.p50_ms)
            ? `P50 ${milliseconds(latest.p50_ms)}`
            : "—",
        ),
      );
      if (latest && finite(latest.max_ms))
        latencyCell.append(
          element(
            "span",
            "cell-subtext",
            `最大 ${milliseconds(latest.max_ms)} · σ ${milliseconds(latest.jitter_ms)}`,
          ),
        );

      const scoreCell = element("td");
      const score = element(
        "span",
        "score-value",
        finite(latest?.score) ? number(latest.score, 1) : "—",
      );
      if (finite(latest?.score))
        score.append(element("span", "score-denominator", "/100"));
      scoreCell.append(score);

      const dateCell = element("td");
      const date = element(
        "span",
        "date-value",
        dateText(latest?.checked_at, true),
      );
      date.title = dateText(latest?.checked_at);
      dateCell.append(date);

      const actionsCell = element("td", "actions-cell");
      const actions = element("div", "row-actions");
      const history = element("button", "text-button", "详情");
      history.type = "button";
      history.id = `candidate-history-${item.id}`;
      history.setAttribute("aria-label", `查看 ${item.endpoint} 的检测历史`);
      history.addEventListener("click", () => showHistory(item));
      const remove = element(
        "button",
        "text-button delete-button",
        state.deleting.has(item.id) ? "删除中" : "删除",
      );
      remove.type = "button";
      remove.id = `candidate-delete-${item.id}`;
      remove.disabled = Boolean(activeJob()) || state.deleting.has(item.id);
      remove.title = activeJob()
        ? "任务运行期间不能删除候选"
        : `删除 ${item.endpoint}`;
      remove.setAttribute("aria-label", `删除候选 ${item.endpoint}`);
      remove.addEventListener("click", () => deleteCandidate(item));
      actions.append(history, remove);
      actionsCell.append(actions);
      row.append(
        selectCell,
        endpointCell,
        countryCell,
        statusCell,
        rateCell,
        latencyCell,
        scoreCell,
        dateCell,
        actionsCell,
      );
      fragment.append(row);
    }
    $("candidates-body").replaceChildren(fragment);
    if (focused)
      document.getElementById(focused)?.focus({ preventScroll: true });
    $("candidate-count").textContent = number(state.items.length);
    $("candidate-empty").hidden = items.length > 0;
    const isFiltered = state.items.length > 0 && items.length === 0;
    $("empty-title").textContent = !state.loaded
      ? state.needsAuth
        ? "输入管理密钥后查看候选"
        : "正在读取候选池"
      : isFiltered
        ? "没有匹配的候选"
        : "候选池还是空的";
    $("empty-description").textContent = !state.loaded
      ? state.needsAuth
        ? "点击页面右上角“管理密钥”连接服务。"
        : "连接成功后，候选和最近检测结果会显示在这里。"
      : isFiltered
        ? "试试其他地区、关键词或状态筛选。"
        : "粘贴一组地址，或从远程来源拉取候选，然后开始检测。";
    $("empty-action").hidden = !state.loaded || isFiltered;
    $("table-count").textContent = items.length
      ? `显示 ${first + 1}–${Math.min(first + PAGE_SIZE, items.length)} / ${number(items.length)} 个候选${state.selected.size ? ` · 已选 ${number(state.selected.size)}` : ""}`
      : isFiltered
        ? `共 ${number(state.items.length)} 个候选，无匹配结果`
        : "暂无候选";
    $("page-label").textContent = `${state.page} / ${pages}`;
    $("page-previous").disabled = state.page <= 1;
    $("page-next").disabled = state.page >= pages;
    const selectedOnPage = pageItems.filter((item) =>
      state.selected.has(item.id),
    ).length;
    $("select-all").checked =
      pageItems.length > 0 && selectedOnPage === pageItems.length;
    $("select-all").indeterminate =
      selectedOnPage > 0 && selectedOnPage < pageItems.length;
    $("select-all").disabled = pageItems.length === 0;
    $("export-txt").disabled = !state.loaded || state.items.length === 0;
    $("export-csv").disabled = !state.loaded || state.items.length === 0;
  }

  function renderProxyControls() {
    const useProxy = $("fetch-use-proxy").checked;
    const configured = Boolean(state.status?.fetch_proxy_configured);
    const busy = state.sourceFetches.size > 0 || geoUpdating();
    $("fetch-proxy-fields").hidden = !useProxy;
    $("fetch-use-proxy").disabled = busy;
    $("fetch-proxy-url").disabled = !useProxy || busy;
    $("fetch-proxy-show").disabled = !useProxy || busy;
    $("fetch-proxy-status").textContent = configured
      ? "后端默认代理已配置"
      : "未配置默认代理";
    $("fetch-proxy-default").textContent = configured
      ? "留空时使用后端默认代理；填写地址则仅用于本次拉取。"
      : "未配置后端默认代理，请填写地址。";
  }

  function renderSources() {
    renderProxyControls();
    const fragment = document.createDocumentFragment();
    for (const source of state.sources) {
      const card = element("div", "source-card");
      const info = element("div", "source-info");
      const name = element("div", "source-name", source.name);
      const url = element("span", "source-url", source.url);
      url.title = source.url;
      const meta = element(
        "p",
        `source-meta ${source.last_error ? "error" : ""}`,
        source.last_error
          ? `上次拉取失败：${source.last_error}`
          : source.last_fetch
            ? `最近拉取 ${dateText(source.last_fetch, true)} · ${number(source.last_count ?? 0)} 条`
            : "尚未拉取",
      );
      info.append(name, url, meta);
      const fetchButton = element(
        "button",
        "button secondary small",
        state.sourceFetches.has(source.id) ? "拉取中…" : "拉取 ↓",
      );
      fetchButton.type = "button";
      fetchButton.disabled = state.sourceFetches.size > 0 || geoUpdating();
      fetchButton.setAttribute("aria-label", `拉取 ${source.name}`);
      fetchButton.addEventListener("click", () => fetchSource(source));
      card.append(info, fetchButton);
      fragment.append(card);
    }
    if (!state.sources.length)
      fragment.append(
        element(
          "p",
          "empty-inline",
          state.loaded
            ? "暂无来源。可添加 HTTPS 文本或 CSV 地址。"
            : "正在读取来源…",
        ),
      );
    $("source-list").replaceChildren(fragment);
    $("source-count").textContent = `${state.sources.length} 个来源`;
  }

  function renderJobs() {
    const fragment = document.createDocumentFragment();
    for (const job of state.jobs) {
      const row = element("div", "job-record");
      const main = element("div", "job-record-main");
      const icon = element(
        "span",
        "job-record-icon",
        job.state === "running" ? "◷" : "↗",
      );
      icon.setAttribute("aria-hidden", "true");
      const text = element("div");
      const title = element(
        "div",
        "job-record-title",
        `${modeName(job.mode)} · ${number(job.total)} 个候选`,
      );
      const sampling = job.config?.samples
        ? ` · ${job.config.samples} 次/目标`
        : "";
      const meta = element(
        "div",
        "job-record-meta",
        `${dateText(job.created_at)}${sampling}`,
      );
      text.append(title, meta);
      if (job.error)
        text.append(
          element(
            "div",
            "job-record-meta",
            `${job.state === "failed" ? "任务异常" : "说明"}：${job.error}`,
          ),
        );
      main.append(icon, text);
      const counts = element(
        "div",
        "job-record-count",
        `已处理 ${number(job.completed)} / ${number(job.total)} · ${number(job.passed)} 个通过`,
      );
      row.append(main, counts, badge(job.state, job.mode));
      fragment.append(row);
    }
    if (!state.jobs.length)
      fragment.append(
        element(
          "p",
          "empty-inline",
          "尚无检测任务。开始检测后，任务记录会显示在这里。",
        ),
      );
    $("jobs-list").replaceChildren(fragment);
  }

  function renderAll() {
    renderOverview();
    renderMode();
    renderTargets();
    renderJob();
    renderGeo();
    renderCountryFilters();
    renderCandidates();
    renderSources();
    renderJobs();
  }

  async function refresh() {
    if (state.refreshing) {
      state.refreshAgain = true;
      return;
    }
    state.refreshing = true;
    $("refresh-button").disabled = true;
    try {
      const status = await request("/api/status");
      state.status = status;
      state.needsAuth = false;
      if (state.mode === "worker" && !status.worker_configured) {
        state.mode = "local";
        state.selected.clear();
        state.loaded = false;
        state.items = [];
      }
      const requestedMode = state.mode;
      const [candidates, sources, jobs] = await Promise.all([
        request(`/api/candidates?mode=${encodeURIComponent(requestedMode)}`),
        request("/api/sources"),
        request("/api/jobs"),
      ]);
      if (requestedMode === state.mode) {
        state.items = candidates.items || [];
        const ids = new Set(
          state.items.filter(matchesCountry).map((item) => item.id),
        );
        for (const id of state.selected)
          if (!ids.has(id)) state.selected.delete(id);
        state.loaded = true;
      } else state.refreshAgain = true;
      state.sources = sources.items || [];
      state.jobs = jobs.items || [];
      setConnection(true);
      $("connection-error").hidden = true;
    } catch (error) {
      if (!(error instanceof AuthRequired)) {
        setConnection(false);
        $("connection-error-text").textContent =
          `服务连接失败：${errorMessage(error)}。已显示的数据可能不是最新结果。`;
        $("connection-error").hidden = false;
      }
    } finally {
      state.refreshing = false;
      $("refresh-button").disabled = false;
      renderAll();
      if (state.refreshAgain && !state.needsAuth) {
        state.refreshAgain = false;
        void refresh();
      }
    }
  }

  function showImportResult(result, label) {
    const root = $("import-result");
    const rejected = result?.rejected || 0;
    const truncated = result?.truncated;
    root.hidden = false;
    root.classList.toggle("has-errors", Boolean(rejected || truncated));
    const fragment = document.createDocumentFragment();
    fragment.append(
      element(
        "p",
        "",
        `${label}：新增 ${number(result?.added ?? 0)} · 重复 ${number(result?.duplicates ?? 0)} · 拒绝 ${number(rejected)}`,
      ),
    );
    if (result?.fetch_via === "direct" || result?.fetch_via === "proxy")
      fragment.append(
        element(
          "p",
          "",
          `拉取路径：${result.fetch_via === "proxy" ? "HTTP 代理" : "直连"}`,
        ),
      );
    if (finite(result?.resolved))
      fragment.append(
        element(
          "p",
          "",
          `解析得到 ${number(result.resolved)} 个地址；候选池共 ${number(result.total_candidates)} 个。`,
        ),
      );
    if (truncated)
      fragment.append(
        element(
          "p",
          "",
          "已触发数量或大小上限，本次仅导入部分内容。请缩小输入范围后重试。",
        ),
      );
    if (result?.errors?.length) {
      const details = element("details");
      details.append(
        element(
          "summary",
          "",
          `查看 ${number(result.errors.length)} 条处理说明`,
        ),
      );
      const list = element("ul");
      for (const error of result.errors.slice(0, 25))
        list.append(
          element(
            "li",
            "",
            typeof error === "string" ? error : JSON.stringify(error),
          ),
        );
      if (result.errors.length > 25)
        list.append(
          element(
            "li",
            "",
            `其余 ${result.errors.length - 25} 条未展开。请分批导入以查看对应错误。`,
          ),
        );
      details.append(list);
      fragment.append(details);
    }
    root.replaceChildren(fragment);
  }

  async function importCandidates(event) {
    event.preventDefault();
    if (state.busyImport) return;
    const text = $("import-text").value.trim();
    if (!text) return;
    if (new Blob([text]).size > 2 * 1024 * 1024) {
      toast("导入文本不能超过 2 MiB，请分批处理。", "error");
      return;
    }
    state.busyImport = true;
    $("import-submit").disabled = true;
    $("import-submit").textContent = "导入中…";
    try {
      const result = await request("/api/import", {
        method: "POST",
        body: { text, source: "手动导入", format: $("import-format").value },
      });
      showImportResult(result, "导入完成");
      toast(`已新增 ${number(result.added)} 个候选`, "success");
      await refresh();
    } catch (error) {
      handleError(error);
    } finally {
      state.busyImport = false;
      $("import-submit").disabled = false;
      $("import-submit").textContent = "导入候选 ↗";
    }
  }

  async function readFile() {
    const file = $("import-file").files?.[0];
    if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024)
        throw new Error("文件不能超过 2 MiB，请拆分文件或使用较小的数据源。");
      $("import-text").value = await file.text();
      $("import-format").value = file.name.toLocaleLowerCase().endsWith(".csv")
        ? "csv"
        : "auto";
      $("file-note").textContent =
        `已读取 ${file.name}（${number(file.size / 1024, 1)} KiB），点击“导入候选”后写入候选池。`;
      $("import-text").focus();
    } catch (error) {
      handleError(error);
    } finally {
      $("import-file").value = "";
    }
  }

  async function addSource(event) {
    event.preventDefault();
    if (state.busySource) return;
    const name = $("source-name").value.trim();
    const url = $("source-url").value.trim();
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password)
        throw new Error();
    } catch {
      toast("请输入不含用户名或密码的 HTTPS 来源地址。", "error");
      return;
    }
    state.busySource = true;
    $("source-submit").disabled = true;
    try {
      await request("/api/sources", {
        method: "POST",
        body: { name, url, format: "auto" },
      });
      $("source-form").reset();
      toast("来源已保存，点击“拉取”获取候选。", "success");
      await refresh();
    } catch (error) {
      handleError(error);
    } finally {
      state.busySource = false;
      $("source-submit").disabled = false;
    }
  }

  function proxyRequestBody() {
    const useProxy = $("fetch-use-proxy").checked;
    const proxyUrl = useProxy ? $("fetch-proxy-url").value.trim() : "";
    if (useProxy && !proxyUrl && !state.status?.fetch_proxy_configured) {
      toast("请填写 HTTP 代理地址，或在后端配置默认代理。", "error");
      $("fetch-proxy-url").focus();
      return null;
    }
    if (proxyUrl) {
      try {
        const parsed = new URL(proxyUrl);
        if (
          parsed.protocol !== "http:" ||
          !parsed.hostname ||
          parsed.hash ||
          parsed.search ||
          !["", "/"].includes(parsed.pathname) ||
          /[\s\u0000-\u001f\u007f]/.test(proxyUrl)
        ) {
          throw new Error();
        }
      } catch {
        toast(
          "代理地址格式应为 http://host:port，可包含 user:pass，不接受路径或查询参数。",
          "error",
        );
        $("fetch-proxy-url").focus();
        return null;
      }
    }
    return { use_proxy: useProxy, proxy_url: proxyUrl };
  }

  async function fetchSource(source) {
    if (state.sourceFetches.size > 0 || geoUpdating()) return;
    const body = proxyRequestBody();
    if (!body) return;
    state.sourceFetches.add(source.id);
    renderSources();
    renderGeo();
    try {
      const result = await request(
        `/api/sources/${encodeURIComponent(source.id)}/fetch`,
        { method: "POST", body },
      );
      showImportResult(result, source.name);
      toast(`${source.name}：新增 ${number(result.added)} 个候选`, "success");
    } catch (error) {
      handleError(error);
    } finally {
      state.sourceFetches.delete(source.id);
      await refresh();
    }
  }

  async function updateGeo() {
    if (!state.status?.geo) {
      renderGeo();
      return;
    }
    if (geoUpdating() || state.sourceFetches.size > 0) return;
    const body = proxyRequestBody();
    if (!body) return;
    state.busyGeo = true;
    state.geoActionError = "";
    renderGeo();
    renderSources();
    schedulePoll();
    try {
      const result = await request("/api/geo/update", { method: "POST", body });
      if (state.status) {
        const geo = result?.geo || result;
        if (geo && typeof geo.ready === "boolean") state.status.geo = geo;
      }
      toast("地区库已更新，正在补充候选归属。", "success");
    } catch (error) {
      if (!(error instanceof AuthRequired))
        state.geoActionError = errorMessage(error);
      handleError(error);
    } finally {
      state.busyGeo = false;
      await refresh();
      renderGeo();
      renderSources();
      schedulePoll();
    }
  }

  async function startJob(event) {
    event.preventDefault();
    if (state.busyJob || activeJob()) return;
    if (!state.selected.size || state.selected.size > MAX_JOB_CANDIDATES) {
      toast(`请先选择 1–${MAX_JOB_CANDIDATES} 个候选地址。`, "error");
      return;
    }
    const candidateIds = [...state.selected];
    state.busyJob = true;
    renderJob();
    try {
      const job = await request("/api/jobs", {
        method: "POST",
        body: {
          candidate_ids: candidateIds,
          mode: state.mode,
          concurrency: Number($("job-concurrency").value),
          samples: Number($("job-samples").value),
          timeout: Number($("job-timeout").value),
        },
      });
      if (state.status) state.status.active_job = job;
      toast(
        `${modeName(job.mode)}已开始，共 ${number(job.total)} 个候选。`,
        "success",
      );
      await refresh();
      schedulePoll();
    } catch (error) {
      handleError(error);
    } finally {
      state.busyJob = false;
      renderJob();
    }
  }

  async function cancelJob() {
    const job = activeJob();
    if (!job || state.busyCancel) return;
    state.busyCancel = true;
    renderJob();
    try {
      await request(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {
        method: "POST",
      });
      toast("已取消任务，完整完成的候选结果仍会保留。");
      await refresh();
      schedulePoll();
    } catch (error) {
      handleError(error);
    } finally {
      state.busyCancel = false;
      renderJob();
    }
  }

  async function deleteCandidate(item) {
    if (state.deleting.has(item.id) || activeJob()) return;
    state.deleting.add(item.id);
    renderCandidates();
    try {
      await request(`/api/candidates/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
      });
      state.selected.delete(item.id);
      toast(`已删除 ${item.endpoint}`);
      await refresh();
    } catch (error) {
      handleError(error);
    } finally {
      state.deleting.delete(item.id);
      renderCandidates();
    }
  }

  async function exportCandidates(format) {
    const button = $(format === "csv" ? "export-csv" : "export-txt");
    const exportedMode = state.mode;
    const exportedCountry = state.country;
    const exportedBasis = state.countryBasis;
    const maxAge = $("export-age").value;
    button.disabled = true;
    try {
      const response = await request(
        `/api/export?mode=${encodeURIComponent(exportedMode)}&format=${format}&max_age_hours=${encodeURIComponent(maxAge)}&country=${encodeURIComponent(exportedCountry)}&country_basis=${encodeURIComponent(exportedBasis)}`,
        { raw: true },
      );
      const blob = await response.blob();
      if (
        !blob.size ||
        (format === "csv" &&
          (await blob.text()).trim().split(/\r?\n/).length < 2)
      ) {
        toast("当前模式和地区下没有在有效期内完整通过的候选。");
        return;
      }
      const url = URL.createObjectURL(blob);
      const link = element("a");
      link.href = url;
      link.download = `proxyip-${exportedMode}-${exportedBasis}-${exportedCountry}-${new Date().toISOString().slice(0, 10)}.${format}`;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      const region =
        exportedCountry === "all" ? "全部地区" : countryLabel(exportedCountry);
      toast(
        `${modeName(exportedMode)} · ${region}已导出为 ${format.toUpperCase()}`,
      );
    } catch (error) {
      handleError(error);
    } finally {
      button.disabled = !state.loaded || !state.items.length;
    }
  }

  function stageName(stage) {
    return (
      {
        dns: "DNS",
        tcp: "TCP",
        tls: "TLS",
        http: "HTTP",
        trace: "响应校验",
        websocket: "WebSocket",
        timeout: "超时",
        cancelled: "取消",
        connect: "连接",
        validation: "参数校验",
        protocol: "通道协议",
      }[stage] ||
      stage ||
      "未知阶段"
    );
  }

  function historyEntry(summary) {
    const entry = element("article", "history-entry");
    const heading = element("div", "history-entry-heading");
    const title = element("div");
    title.append(
      badge(summary.status, summary.mode),
      element("span", "badge neutral", modeName(summary.mode)),
    );
    heading.append(
      title,
      element("span", "date-value", dateText(summary.checked_at)),
    );
    const metrics = element("div", "history-metrics");
    for (const [label, value] of [
      [
        "成功率 / 已测样本",
        `${summary.attempts ? percentage(summary.success_rate) : "—"} · ${summary.attempts || 0}/${summary.expected_samples || 0}`,
      ],
      ["P50 · ms", milliseconds(summary.p50_ms)],
      ["最大 · ms", milliseconds(summary.max_ms)],
      ["波动 σ · ms", milliseconds(summary.jitter_ms)],
      [
        "综合评分",
        finite(summary.score) ? `${number(summary.score, 1)} / 100` : "—",
      ],
    ]) {
      const metric = element("div");
      metric.append(
        element("span", "history-metric-label", label),
        element("span", "history-metric-value", value),
      );
      metrics.append(metric);
    }
    entry.append(heading, metrics);
    if (summary.mode === "local")
      entry.append(
        element(
          "p",
          "history-context",
          "检测位置：当前本机 / VPS；此记录不能证明 Workers 路径可用。",
        ),
      );
    if (summary.worker_colos?.length)
      entry.append(
        element(
          "p",
          "history-context",
          `Worker 机房：${summary.worker_colos.join(" / ")}`,
        ),
      );
    if (summary.exit_ips?.length)
      entry.append(
        element(
          "p",
          "history-context",
          `目标观测 IP：${summary.exit_ips.join(" / ")}`,
        ),
      );
    const observed = exitCountries(summary);
    entry.append(
      element(
        "p",
        "history-context",
        `观测出口地区${observed.length > 1 ? "（多地区）" : ""}：${observed.length ? observed.map((code) => countryLabel(code)).join(" / ") : "未知/未分类"}。${observed.length ? "每个已知地区至少出现于一次成功样本。" : ""}${summary.country_unknown_samples ? `${number(summary.country_unknown_samples)} 个成功样本地区未知。` : ""}`,
      ),
    );
    if (summary.last_error)
      entry.append(element("p", "history-error", summary.last_error));
    const samples = summary.samples || [];
    const details = element("details");
    details.append(element("summary", "", `逐次探测（${samples.length} 条）`));
    if (!samples.length)
      details.append(
        element(
          "p",
          "empty-inline",
          "没有已保存的样本；任务可能尚未开始或已中断。",
        ),
      );
    else {
      const scroller = element("div", "table-scroll");
      const table = element("table", "sample-table");
      const head = element("thead");
      const headRow = element("tr");
      for (const label of [
        "目标 / 路径",
        "结果",
        "TCP",
        "TLS",
        "TTFB",
        "总耗时",
        "目标观测 / Worker",
      ])
        headRow.append(element("th", "", label));
      head.append(headRow);
      const body = element("tbody");
      for (const sample of samples) {
        const row = element("tr");
        const targetCell = element("td", "", sample.target || "—");
        targetCell.append(
          element(
            "span",
            "cell-subtext",
            modeName(sample.mode || summary.mode),
          ),
        );
        row.append(
          targetCell,
          element(
            "td",
            "sample-status",
            sample.ok ? "通过" : `${stageName(sample.stage)}失败`,
          ),
        );
        for (const timing of [
          sample.tcp_ms,
          sample.tls_ms,
          sample.ttfb_ms,
          sample.total_ms,
        ])
          row.append(element("td", "", milliseconds(timing)));
        const locationCell = element("td", "", sample.exit_ip || "—");
        locationCell.append(
          element(
            "span",
            "cell-subtext long",
            `观测出口：${countryLabel(sample.exit_country)}`,
          ),
        );
        locationCell.append(
          element(
            "span",
            "cell-subtext long",
            `目标 colo：${sample.exit_colo || "—"} / Worker：${sample.worker_colo || "—"}`,
          ),
        );
        row.append(locationCell);
        body.append(row);
        if (sample.error) {
          const errorRow = element("tr", "sample-error");
          const errorCell = element("td", "", sample.error);
          errorCell.colSpan = 7;
          errorRow.append(errorCell);
          body.append(errorRow);
        }
      }
      table.append(head, body);
      scroller.append(table);
      details.append(scroller);
      details.append(
        element(
          "p",
          "field-hint",
          "耗时单位为毫秒。Workers 模式的 TCP 耗时在 Worker 内测量，其余阶段包含隧道往返。",
        ),
      );
    }
    entry.append(details);
    return entry;
  }

  async function showHistory(item) {
    const sequence = ++state.historyRequest;
    $("history-title").textContent = item.endpoint;
    $("history-content").replaceChildren(
      element("p", "empty-inline", "正在读取检测历史…"),
    );
    if (!$("history-dialog").open) $("history-dialog").showModal();
    try {
      const result = await request(
        `/api/candidates/${encodeURIComponent(item.id)}/history`,
      );
      if (sequence !== state.historyRequest) return;
      const fragment = document.createDocumentFragment();
      for (const summary of result.items || [])
        fragment.append(historyEntry(summary));
      if (!result.items?.length)
        fragment.append(
          element(
            "p",
            "empty-inline",
            "这个候选尚无检测记录。选择它并开始检测后，可在此查看每次样本。",
          ),
        );
      $("history-content").replaceChildren(fragment);
    } catch (error) {
      if (sequence === state.historyRequest)
        $("history-content").replaceChildren(
          element("p", "history-error", errorMessage(error)),
        );
    }
  }

  async function authenticate(event) {
    event.preventDefault();
    const token = $("auth-token").value.trim();
    if (!token) return;
    $("auth-submit").disabled = true;
    $("auth-error").hidden = true;
    state.token = token;
    try {
      await request("/api/status", { authPrompt: false });
      state.needsAuth = false;
      $("auth-token").value = "";
      $("auth-dialog").close();
      toast("已连接服务，密钥仅保存在当前页面。", "success");
      await refresh();
    } catch (error) {
      state.token = "";
      $("auth-error").textContent = errorMessage(error);
      $("auth-error").hidden = false;
      $("auth-token").focus();
    } finally {
      $("auth-submit").disabled = false;
    }
  }

  async function changeMode(mode) {
    if (
      mode === state.mode ||
      (mode === "worker" && !state.status?.worker_configured)
    )
      return;
    state.mode = mode;
    state.selected.clear();
    state.items = [];
    state.loaded = false;
    state.page = 1;
    renderAll();
    await refresh();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(
      async () => {
        if (!state.needsAuth && !document.hidden) await refresh();
        schedulePoll();
      },
      activeJob() || geoUpdating() ? 3000 : 15000,
    );
  }

  function selectFirstCandidates(count) {
    if (!state.loaded) return;
    if (!Number.isInteger(count) || count < 1 || count > MAX_JOB_CANDIDATES) {
      toast(`请选择 1–${MAX_JOB_CANDIDATES} 之间的整数。`, "error");
      $("selection-count").focus();
      return;
    }
    const candidates = filteredItems().slice(0, count);
    state.selected = new Set(candidates.map((candidate) => candidate.id));
    state.page = 1;
    renderCandidates();
    renderJob();
    toast(
      candidates.length < count
        ? `当前筛选仅有 ${number(candidates.length)} 个候选，已全部选择并替换原选择。`
        : `已按当前筛选和排序跨页选择前 ${number(candidates.length)} 个候选，替换原选择。`,
    );
  }

  $("import-form").addEventListener("submit", importCandidates);
  $("import-file").addEventListener("change", readFile);
  $("source-form").addEventListener("submit", addSource);
  $("fetch-use-proxy").addEventListener("change", () => {
    renderProxyControls();
    renderGeo();
  });
  $("geo-update").addEventListener("click", updateGeo);
  $("fetch-proxy-show").addEventListener("click", () => {
    const show = $("fetch-proxy-url").type === "password";
    $("fetch-proxy-url").type = show ? "text" : "password";
    $("fetch-proxy-show").textContent = show ? "隐藏" : "显示";
    $("fetch-proxy-show").setAttribute("aria-pressed", String(show));
  });
  $("job-form").addEventListener("submit", startJob);
  $("job-cancel").addEventListener("click", cancelJob);
  $("job-samples").addEventListener("input", renderTargets);
  $("mode-local").addEventListener("click", () => changeMode("local"));
  $("mode-worker").addEventListener("click", () => changeMode("worker"));
  $("refresh-button").addEventListener("click", refresh);
  $("retry-connect").addEventListener("click", refresh);
  $("select-first-form").addEventListener("submit", (event) => {
    event.preventDefault();
    selectFirstCandidates(Number($("selection-count").value));
  });
  document.querySelectorAll("[data-select-count]").forEach((button) => {
    button.addEventListener("click", () => {
      const count = Number(button.dataset.selectCount);
      $("selection-count").value = String(count);
      selectFirstCandidates(count);
    });
  });
  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    renderCandidates();
    renderJob();
  });
  for (const id of ["search-input", "status-filter", "sort-filter"])
    $(id).addEventListener(id === "search-input" ? "input" : "change", () => {
      state.page = 1;
      renderCandidates();
    });
  for (const id of ["country-filter", "country-basis"]) {
    $(id).addEventListener("change", () => {
      state.country = $("country-filter").value;
      state.countryBasis = $("country-basis").value;
      const cleared = state.selected.size;
      state.selected.clear();
      state.page = 1;
      renderCountryFilters();
      renderCandidates();
      renderJob();
      if (cleared) toast("地区筛选已改变，已清空之前的选择。");
    });
  }
  $("select-all").addEventListener("change", () => {
    const checked = $("select-all").checked;
    const first = (state.page - 1) * PAGE_SIZE;
    for (const item of filteredItems().slice(first, first + PAGE_SIZE)) {
      if (checked) state.selected.add(item.id);
      else state.selected.delete(item.id);
    }
    renderCandidates();
    renderJob();
  });
  $("page-previous").addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    renderCandidates();
  });
  $("page-next").addEventListener("click", () => {
    state.page += 1;
    renderCandidates();
  });
  $("export-txt").addEventListener("click", () => exportCandidates("txt"));
  $("export-csv").addEventListener("click", () => exportCandidates("csv"));
  $("auth-open").addEventListener("click", () => showAuth(state.needsAuth));
  $("auth-close").addEventListener("click", () => $("auth-dialog").close());
  $("auth-dialog").addEventListener("close", () => {
    $("auth-token").value = "";
  });
  $("auth-form").addEventListener("submit", authenticate);
  $("auth-clear").addEventListener("click", () => {
    state.token = "";
    state.needsAuth = false;
    state.items = [];
    state.sources = [];
    state.jobs = [];
    state.status = null;
    state.loaded = false;
    state.selected.clear();
    $("auth-token").value = "";
    $("auth-dialog").close();
    renderAll();
    toast("当前页面中的管理密钥已清除。");
    void refresh();
  });
  $("history-close").addEventListener("click", () =>
    $("history-dialog").close(),
  );
  for (const dialog of [$("auth-dialog"), $("history-dialog")])
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        const bounds = dialog.getBoundingClientRect();
        if (
          event.clientX < bounds.left ||
          event.clientX > bounds.right ||
          event.clientY < bounds.top ||
          event.clientY > bounds.bottom
        )
          dialog.close();
      }
    });
  document.querySelectorAll(".nav-link").forEach((link) =>
    link.addEventListener("click", () => {
      document
        .querySelectorAll(".nav-link")
        .forEach((item) => item.classList.toggle("active", item === link));
    }),
  );
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.needsAuth) void refresh();
  });
  renderAll();
  void refresh().finally(schedulePoll);
})();
