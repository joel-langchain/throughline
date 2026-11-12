"use strict";

// Read-only by construction: this page only reads the JSON files the weekly run
// writes to data/. Report text comes from web search, so it is sanitised before
// it is rendered, and every other string goes in via textContent.

const NS = "http://www.w3.org/2000/svg";
const state = { runs: [], byDate: new Map(), reports: new Map(), selected: null };
// Motion is on unless the viewer's system asks for less. Every animation on the
// page hangs off the `motion` class, so without it the page is simply static.
const MOTION = window.matchMedia("(prefers-reduced-motion: no-preference)").matches
  && "IntersectionObserver" in window;
if (MOTION) document.documentElement.classList.add("motion");

// --- small helpers --------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function s(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const fmt = {
  usd: (v) => (v == null ? "–" : `$${v.toFixed(2)}`),
  min: (v) => (v == null ? "–" : `${Math.round(v)} min`),
  pct: (v) => (v == null ? "–" : `${Math.round(v * 100)}%`),
  int: (v) => (v == null ? "–" : String(Math.round(v))),
};

function shortDate(iso) {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric", month: "short", timeZone: "UTC",
  });
}

function longDate(iso) {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

function median(values) {
  const v = values.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

const score = (run, key) => (run.scores && run.scores[key] != null ? run.scores[key] : null);

// A clean axis: a 1, 2, or 5 step, about four ticks, top tick at or above max.
function niceScale(max) {
  if (max <= 0) return { yMax: 1, step: 1 };
  const raw = max / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((v) => v >= raw);
  return { yMax: Math.ceil(max / step) * step, step };
}

// --- summary tiles -------------------------------------------------------------

function renderTiles() {
  const published = state.runs.filter((r) => r.published);
  const held = state.runs.filter((r) => !r.published);
  const latest = published[published.length - 1];
  const tiles = [
    ["Reports published", String(published.length), `since ${shortDate(state.runs[0].date)}`],
    ["Scheduled runs with no report", String(held.length), "shown in grey below"],
    ["Median cost per report", fmt.usd(median(published.map((r) => r.cost_usd))), "model calls only"],
    ["Spent to date", fmt.usd(state.runs.reduce((sum, r) => sum + (r.cost_usd || 0), 0)),
      `model calls, ${state.runs.length} runs`],
    ["Median run time", fmt.min(median(published.map((r) => r.duration_minutes))), "start to published"],
    ["Sources in the latest report", fmt.int(latest && score(latest, "source_count")), latest ? shortDate(latest.date) : ""],
  ];
  const lastSplit = [...published].reverse().find((r) => r.cost_by_stage && r.cost_usd);
  if (lastSplit) {
    const share = stageSplit(lastSplit)["citation-verifier"] / lastSplit.cost_usd;
    tiles.push(["Citation checking's share of cost", fmt.pct(share), `${shortDate(lastSplit.date)} run`]);
  }
  const corroborated = latest && score(latest, "claims_corroborated");
  if (corroborated != null) {
    tiles.push(["Claims corroborated, latest", fmt.pct(corroborated), "primary source or two independent ones"]);
  }
  const root = document.getElementById("tiles");
  root.replaceChildren(...tiles.map(([label, value, note]) =>
    h("div", { class: "card tile" },
      h("div", { class: "label" }, label),
      h("div", { class: "value" }, value),
      h("div", { class: "note" }, note))));
}

// --- reports -----------------------------------------------------------------

// The newest week with a report. The page opens on it.
function latestReport() {
  return [...state.runs].reverse().find((r) => r.published);
}

// How long ago a run's date was, in the viewer's own words: "today", "3 days ago".
function age(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const days = Math.floor((Date.now() - Date.UTC(y, m - 1, d)) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

function renderWeeks() {
  const root = document.getElementById("weeks");
  const newest = [...state.runs].reverse();
  const latest = latestReport();
  root.replaceChildren(...newest.map((run) => {
    const isLatest = latest && run.date === latest.date;
    return h("button", {
      type: "button",
      class: run.published ? "" : "held",
      "aria-pressed": "false",
      "data-date": run.date,
      "aria-label": `${longDate(run.date)}${isLatest ? ", latest report" : ""}${run.published ? "" : ", no report"}`,
      onclick: () => select(run.date),
    }, shortDate(run.date), isLatest ? h("span", { class: "latest-mark", "aria-hidden": "true" }, "Latest") : null);
  }));
}

async function reportFor(date) {
  if (!state.reports.has(date)) {
    const res = await fetch(`data/runs/${date}.json`, { cache: "no-cache" });
    state.reports.set(date, (await res.json()).report || "");
  }
  return state.reports.get(date);
}

function renderMarkdown(target, markdown) {
  if (window.marked && window.DOMPurify) {
    target.innerHTML = window.DOMPurify.sanitize(window.marked.parse(markdown));
    for (const a of target.querySelectorAll("a")) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  } else {
    target.replaceChildren(h("pre", { style: "white-space: pre-wrap" }, markdown));
  }
}

async function select(date) {
  state.selected = date;
  const run = state.byDate.get(date);
  for (const b of document.querySelectorAll("#weeks button")) {
    b.setAttribute("aria-pressed", String(b.dataset.date === date));
  }
  const header = document.getElementById("week-meta");
  const body = document.getElementById("report");
  const isLatest = latestReport()?.date === date;
  header.replaceChildren(
    h("div", { class: "week-title" },
      h("h3", {}, `Week of ${fullDate(date)}`),
      isLatest ? h("span", { class: "tag latest" }, "Latest") : null,
      run.published ? null : h("span", { class: "tag held-tag" }, "No report")),
    h("p", { class: "week-sub" }, run.published
      ? `Published ${weekday(date)}, ${age(date)}`
      : `The scheduled run on ${weekday(date)} did not publish a report`));

  if (!run.published) {
    // The reason's first line is the alert's headline; the rest is what went wrong.
    const [, ...detail] = (run.held_reason || "").split("\n");
    body.replaceChildren(
      h("div", { class: "r-lede" },
        h("p", { class: "held-note" }, detail.join("\n") || "The run did not produce a report.")),
      h("aside", { class: "r-aside" }, factsOf(run)));
    replay(body, "swap");
    return;
  }
  body.replaceChildren(h("p", { class: "r-lede loading" }, "Loading…"));
  const markdown = await reportFor(date);
  if (state.selected !== date) return;
  body.replaceChildren(...articleOf(run, markdown));
  replay(body, "swap");
}

function fullDate(iso) {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

function weekday(iso) {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "long", timeZone: "UTC" });
}

// Restart a one-shot CSS animation on an element.
function replay(el, cls) {
  if (!MOTION) return;
  el.classList.remove(cls);
  void el.getBoundingClientRect();
  el.classList.add(cls);
}

// The week's numbers, for the sidebar. Cost and run time arrive a week late.
function factsOf(run) {
  const later = (v, f) => (v == null ? h("span", { class: "pending" }, "Added next week") : f(v));
  const rows = [];
  if (run.published) {
    const fresh = run.topics.filter((t) => t.status === "new").length;
    const developing = run.topics.length - fresh;
    rows.push(["Topics", h("span", {}, String(run.topics.length),
      h("span", { class: "fact-sub" }, `${fresh} new, ${developing} developing`))]);
    rows.push(["Sources cited", fmt.int(score(run, "source_count"))]);
  }
  rows.push(["Run time", later(run.duration_minutes, fmt.min)]);
  rows.push(["Model cost", later(run.cost_usd, fmt.usd)]);
  return h("dl", { class: "facts" }, ...rows.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v))));
}

function flash(el) {
  if (!el) return;
  el.classList.remove("flash");
  void el.getBoundingClientRect();
  el.classList.add("flash");
}

// Wrap each topic of the rendered report in its own block, led by its number and
// its new/developing tag (taken out of the heading text), and the sources in another.
function sectionize(full, run) {
  const blocks = [];
  let current = null;
  for (const node of [...full.childNodes]) {
    if (node.nodeName === "H2") {
      const text = node.textContent.trim();
      const isSources = /^sources\b/i.test(text);
      current = h("section", { class: isSources ? "sources-block" : "topic-block" });
      full.insertBefore(current, node);
      if (!isSources) {
        const n = blocks.length;
        const m = text.match(/^(.*?)\s*\((new|developing)\)\s*$/i);
        const status = (m && m[2].toLowerCase()) || (run.topics[n] && run.topics[n].status) || null;
        node.textContent = m ? m[1] : text;
        current.append(h("p", { class: "topic-eyebrow" },
          h("span", { class: "num" }, String(n + 1)),
          status ? h("span", { class: `tag ${status}` }, status) : null));
        blocks.push(current);
      }
      current.append(node);
    } else if (current) {
      current.append(node);
    }
  }
  // Number the sources so a citation can point at one.
  const list = full.querySelector(".sources-block ol");
  if (list) {
    const first = Number(list.getAttribute("start") || 1);
    [...list.children].forEach((li, i) => { li.id = `src-${first + i}`; });
  }
  return blocks;
}

// Turn "[18]" in the text into a small link to source 18.
function linkCitations(full) {
  const walker = document.createTreeWalker(full, NodeFilter.SHOW_TEXT);
  const hits = [];
  while (walker.nextNode()) {
    const t = walker.currentNode;
    if (/\[\d+\]/.test(t.nodeValue) && !t.parentElement.closest("a, .sources-block")) hits.push(t);
  }
  for (const t of hits) {
    const text = t.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of text.matchAll(/\s?\[(\d+)\]/g)) {
      frag.append(text.slice(last, m.index));
      const id = `src-${m[1]}`;
      frag.append(h("a", {
        class: "cite", href: `#${id}`, "aria-label": `Source ${m[1]}`,
        onclick: () => setTimeout(() => flash(document.getElementById(id)), MOTION ? 350 : 0),
      }, m[1]));
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    t.replaceWith(frag);
  }
}

// Highlight the sidebar topic whose section is being read.
function spy(blocks, links) {
  if (!("IntersectionObserver" in window) || !blocks.length) return;
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const n = blocks.indexOf(e.target);
      links.forEach((l, i) => l.classList.toggle("current", i === n));
    }
  }, { rootMargin: "-35% 0px -60% 0px" });
  blocks.forEach((b) => io.observe(b));
}

// A week reads as an article: the opening paragraphs, then the full report on
// request, with the topics and the week's numbers in a sidebar that stays in view.
function articleOf(run, markdown) {
  const at = markdown.search(/^##\s/m);
  // The report's own title line repeats the date the header already shows.
  const head = (at < 0 ? markdown : markdown.slice(0, at)).replace(/^#\s.*\n+/, "");
  const rest = at < 0 ? "" : markdown.slice(at);

  const lede = h("div", { class: "r-lede summary" });
  renderMarkdown(lede, head);
  const full = h("div", { class: "r-full full", id: "full-report", hidden: "" });
  const toggle = h("button", {
    type: "button", class: "expand", "aria-expanded": "false", "aria-controls": "full-report",
    onclick: () => setOpen(toggle.getAttribute("aria-expanded") !== "true"),
  }, "Read the full report");
  let blocks = [];
  function setOpen(open) {
    if (open && !full.hasChildNodes()) {
      renderMarkdown(full, rest);
      blocks = sectionize(full, run);
      linkCitations(full);
      spy(blocks, links);
    }
    full.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Hide the full report" : "Read the full report";
    if (!open) links.forEach((l) => l.classList.remove("current"));
  }
  // A topic opens the full report at its own section.
  function jumpTo(n) {
    setOpen(true);
    const target = blocks[n];
    if (!target) return;
    target.scrollIntoView({ behavior: MOTION ? "smooth" : "auto", block: "start" });
    flash(target.querySelector("h2"));
  }

  const aside = h("aside", { class: "r-aside" });
  const links = [];
  if (run.topics.length) {
    aside.append(
      h("p", { class: "aside-label" }, "In this report"),
      h("ol", { class: "topics" }, ...run.topics.map((t, n) => {
        const inner = [
          h("span", { class: "num", "aria-hidden": "true" }, String(n + 1)),
          h("span", { class: "topic-text" },
            h("span", { class: "topic-name" }, t.name),
            t.status ? h("span", { class: `tag ${t.status}` }, t.status) : null),
        ];
        const link = rest
          ? h("button", { type: "button", class: "topic-link", onclick: () => jumpTo(n) }, ...inner)
          : h("span", { class: "topic-link static" }, ...inner);
        links.push(link);
        return h("li", {}, link);
      })));
  }
  aside.append(h("p", { class: "aside-label" }, "This week's run"), factsOf(run));

  const parts = [lede, aside];
  if (rest) parts.push(h("div", { class: "r-actions" }, toggle), full);
  return parts;
}

// --- charts ----------------------------------------------------------------------

const M = { top: 16, right: 14, bottom: 26, left: 38 };
const PLOT_H = 150;

function chartCard({ title, sub, legend }) {
  const plot = h("div", { class: "plot" });
  const card = h("div", { class: "card chart" },
    h("h3", {}, title),
    h("p", { class: "sub" }, sub),
    legend && legend.length
      ? h("div", { class: "legend" }, ...legend.map(([label, color, kind]) =>
          h("span", {}, h("span", { class: `key ${kind || ""}`, style: `background:${color}` }), label)))
      : null,
    plot);
  return { card, plot };
}

function frame(plot, { yMax, step, yFormat, label }) {
  const width = Math.max(plot.clientWidth, 260);
  const svg = s("svg", {
    viewBox: `0 0 ${width} ${PLOT_H + M.top + M.bottom}`,
    height: PLOT_H + M.top + M.bottom, role: "group", "aria-label": label,
  });
  const innerW = width - M.left - M.right;
  const y = (v) => M.top + PLOT_H - (v / yMax) * PLOT_H;
  for (let i = 0; i * step <= yMax + 1e-9; i++) {
    const v = i * step;
    s("line", {
      x1: M.left, x2: width - M.right, y1: y(v), y2: y(v),
      stroke: i === 0 ? css("--axis") : css("--grid"), "stroke-width": 1,
    }, svg);
    const t = s("text", { x: M.left - 6, y: y(v) + 4, "text-anchor": "end" }, svg);
    t.textContent = yFormat(v);
  }
  const n = state.runs.length;
  const band = innerW / n;
  const xc = (i) => M.left + band * i + band / 2;
  // A date label ("14 Sept") needs about 46px; show every nth so none collide.
  const every = Math.max(1, Math.ceil(46 / band));
  state.runs.forEach((run, i) => {
    if ((n - 1 - i) % every) return;
    const t = s("text", { x: xc(i), y: M.top + PLOT_H + 18, "text-anchor": "middle" }, svg);
    t.textContent = shortDate(run.date);
  });
  const tip = h("div", { class: "tip", hidden: "" });
  plot.replaceChildren(svg, tip);
  return { svg, tip, y, xc, band, width };
}

function showTip(ctx, i, rows) {
  const { tip, xc, width } = ctx;
  const run = state.runs[i];
  tip.replaceChildren(
    h("div", { class: "when" }, `${shortDate(run.date)}${run.published ? "" : ", no report"}`),
    ...rows.map(([color, value, label]) =>
      h("div", { class: "row" },
        h("span", { class: "lk", style: `background:${color}` }),
        h("strong", {}, value),
        h("span", {}, label))));
  tip.hidden = false;
  const left = Math.min(Math.max(xc(i) - tip.offsetWidth / 2, 0), width - tip.offsetWidth);
  tip.style.left = `${left}px`;
  tip.style.top = "0px";
}

function hitBands(ctx, onShow, labelFor) {
  const { svg, band, tip } = ctx;
  state.runs.forEach((run, i) => {
    const r = s("rect", {
      class: "hit", x: M.left + band * i, y: M.top, width: band, height: PLOT_H,
      fill: "transparent", tabindex: "0", "aria-label": labelFor(i),
    }, svg);
    const show = () => onShow(i);
    r.addEventListener("pointerenter", show);
    r.addEventListener("focus", show);
    r.addEventListener("pointerleave", () => { tip.hidden = true; onShow(null); });
    r.addEventListener("blur", () => { tip.hidden = true; onShow(null); });
  });
}

function barPath(x, y0, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  const y = y0 - h;
  return `M${x},${y0} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y0} Z`;
}

function columnChart(container, { title, sub, value, format, yFormat, withHeld }) {
  const c1 = css("--series-1");
  const none = css("--none");
  const legend = withHeld ? [["Report published", c1], ["No report", none]] : null;
  const { card, plot } = chartCard({ title, sub, legend });
  container.append(card);
  const values = state.runs.map((r) => (withHeld || r.published ? value(r) : null));
  const ctx = frame(plot, { ...niceScale(Math.max(...values.filter((v) => v != null), 0)), yFormat, label: title });
  const bw = Math.min(24, ctx.band * 0.6);
  const marks = values.map((v, i) => {
    if (v == null) return null;
    const color = state.runs[i].published ? c1 : none;
    return s("path", {
      class: "mark", d: barPath(ctx.xc(i) - bw / 2, ctx.y(0), bw, ctx.y(0) - ctx.y(v), 4), fill: color,
      style: `--i:${i}`,
    }, ctx.svg);
  });
  const last = values.length - 1;
  if (values[last] != null) {
    const t = s("text", { class: "direct", x: ctx.xc(last), y: ctx.y(values[last]) - 6, "text-anchor": "middle" }, ctx.svg);
    t.textContent = format(values[last]);
  }
  hitBands(ctx, (i) => {
    marks.forEach((m, j) => m && m.classList.toggle("active", j === i));
    if (i == null) return;
    const run = state.runs[i];
    showTip(ctx, i, [[run.published ? c1 : none, values[i] == null ? "No report" : format(values[i]), ""]]);
  }, (i) => `${shortDate(state.runs[i].date)}: ${values[i] == null ? "no report" : format(values[i])}`);
}

function stackedTopics(container) {
  const c1 = css("--series-1");
  const c2 = css("--series-2");
  const { card, plot } = chartCard({
    title: "Topics per report",
    sub: "New stories, and storylines carried over from earlier weeks",
    legend: [["New", c1], ["Developing", c2]],
  });
  container.append(card);
  const counts = state.runs.map((r) => (r.published
    ? { fresh: r.topics.filter((t) => t.status === "new").length,
        dev: r.topics.filter((t) => t.status === "developing").length }
    : null));
  const ctx = frame(plot, {
    ...niceScale(Math.max(...counts.filter(Boolean).map((c) => c.fresh + c.dev), 0)),
    yFormat: (v) => fmt.int(v), label: "Topics per report",
  });
  const bw = Math.min(24, ctx.band * 0.6);
  const marks = counts.map((c, i) => {
    if (!c) return null;
    const x = ctx.xc(i) - bw / 2;
    const base = ctx.y(0);
    const hNew = base - ctx.y(c.fresh);
    const hDev = base - ctx.y(c.dev);
    const g = s("g", { class: "mark", style: `--i:${i}` }, ctx.svg);
    // New sits on the baseline; developing stacks on top with the rounded end and
    // a 2px surface gap between the two.
    if (c.fresh && c.dev) {
      s("rect", { x, y: base - hNew + 1, width: bw, height: Math.max(hNew - 1, 0), fill: c1 }, g);
      s("path", { d: barPath(x, base - hNew - 1, bw, Math.max(hDev - 1, 0), 4), fill: c2 }, g);
    } else if (c.fresh) {
      s("path", { d: barPath(x, base, bw, hNew, 4), fill: c1 }, g);
    } else if (c.dev) {
      s("path", { d: barPath(x, base, bw, hDev, 4), fill: c2 }, g);
    }
    return g;
  });
  hitBands(ctx, (i) => {
    marks.forEach((m, j) => m && m.classList.toggle("active", j === i));
    if (i == null) return;
    const c = counts[i];
    showTip(ctx, i, c ? [[c1, String(c.fresh), "new"], [c2, String(c.dev), "developing"]] : [[css("--none"), "No report", ""]]);
  }, (i) => (counts[i] ? `${shortDate(state.runs[i].date)}: ${counts[i].fresh} new, ${counts[i].dev} developing` : `${shortDate(state.runs[i].date)}: no report`));
}

// Bottom to top. Anything unexpected (the general-purpose helper, say) is counted
// as editing, since the editor is what called it.
const STAGES = [
  ["citation-verifier", "Citation checking", "--series-1"],
  ["editor", "Editing", "--series-2"],
  ["topic-researcher", "Research", "--series-3"],
  ["final-pass-reviewer", "Final review", "--series-4"],
];

function stageSplit(run) {
  if (!run.cost_by_stage) return null;
  const known = new Set(STAGES.map(([k]) => k));
  const split = Object.fromEntries(STAGES.map(([k]) => [k, run.cost_by_stage[k] || 0]));
  for (const [k, v] of Object.entries(run.cost_by_stage)) if (!known.has(k)) split.editor += v;
  return split;
}

function stackedCost(container) {
  const colors = Object.fromEntries(STAGES.map(([k, , v]) => [k, css(v)]));
  const none = css("--none");
  const { card, plot } = chartCard({
    title: "Cost per run, by stage",
    sub: "Model calls, in US dollars. Search is billed separately and not included.",
    legend: [...STAGES.map(([k, label]) => [label, colors[k]]), ["Not broken down", none]],
  });
  // Full width, set before it draws, since the chart measures its card.
  card.classList.add("wide");
  container.append(card);
  const totals = state.runs.map((r) => r.cost_usd);
  const ctx = frame(plot, {
    ...niceScale(Math.max(...totals.filter((v) => v != null), 0)),
    yFormat: (v) => `$${Math.round(v)}`, label: "Cost per run, by stage",
  });
  const bw = Math.min(24, ctx.band * 0.6);
  const marks = state.runs.map((run, i) => {
    if (run.cost_usd == null) return null;
    const x = ctx.xc(i) - bw / 2;
    const g = s("g", { class: "mark", style: `--i:${i}` }, ctx.svg);
    const split = stageSplit(run);
    // Older traces kept cost on the run total only, so those weeks are one grey bar.
    const parts = split
      ? STAGES.map(([k]) => [colors[k], split[k]]).filter(([, v]) => v > 0)
      : [[none, run.cost_usd]];
    let base = ctx.y(0);
    parts.forEach(([color, v], j) => {
      // Square segments with a 2px surface gap between them; only the top one is rounded.
      const segment = ctx.y(0) - ctx.y(v);
      const gap = j > 0 ? 1 : 0;
      const top = j === parts.length - 1;
      const height = Math.max(segment - gap - (top ? 0 : 1), 0);
      if (top) s("path", { d: barPath(x, base - gap, bw, height, 4), fill: color }, g);
      else s("rect", { x, y: base - gap - height, width: bw, height, fill: color }, g);
      base -= segment;
    });
    return g;
  });
  const last = state.runs.length - 1;
  if (totals[last] != null) {
    const t = s("text", { class: "direct", x: ctx.xc(last), y: ctx.y(totals[last]) - 6, "text-anchor": "middle" }, ctx.svg);
    t.textContent = fmt.usd(totals[last]);
  }
  hitBands(ctx, (i) => {
    marks.forEach((m, j) => m && m.classList.toggle("active", j === i));
    if (i == null) return;
    const run = state.runs[i];
    const split = stageSplit(run);
    const rows = [[css("--axis"), fmt.usd(run.cost_usd), "total"]];
    if (split) {
      for (const [k, label] of [...STAGES].reverse()) {
        if (split[k] > 0) rows.push([colors[k], fmt.usd(split[k]), label]);
      }
    } else if (run.cost_usd != null) {
      rows.push([none, "", "not broken down"]);
    }
    showTip(ctx, i, rows);
  }, (i) => `${shortDate(state.runs[i].date)}: ${fmt.usd(state.runs[i].cost_usd)}`);
}

function lineChart(container, { title, sub, series }) {
  const present = series.filter((sr) => state.runs.some((r) => sr.value(r) != null));
  const legend = present.length > 1 ? present.map((sr) => [sr.label, sr.color, "line"]) : null;
  const { card, plot } = chartCard({ title, sub, legend });
  container.append(card);
  const ctx = frame(plot, { yMax: 1, step: 0.25, yFormat: fmt.pct, label: title });
  const cross = s("line", { y1: M.top, y2: M.top + PLOT_H, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden" }, ctx.svg);
  for (const sr of present) {
    const pts = state.runs.map((r, i) => (sr.value(r) == null ? null : [ctx.xc(i), ctx.y(sr.value(r))]));
    // Break the line at weeks with no value, so a lost week reads as a gap.
    let d = "";
    pts.forEach((p, i) => { if (p) d += `${pts[i - 1] ? "L" : "M"}${p[0]},${p[1]} `; });
    s("path", { class: "line", pathLength: "1", d, fill: "none", stroke: sr.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, ctx.svg);
    pts.forEach((p, i) => {
      const lone = p && !pts[i - 1] && !pts[i + 1];
      const lastPoint = p && pts.slice(i + 1).every((q) => !q);
      if (lone || lastPoint) {
        s("circle", { class: "dot", cx: p[0], cy: p[1], r: 4, fill: sr.color, stroke: css("--surface"), "stroke-width": 2 }, ctx.svg);
      }
      if (lastPoint) {
        // Sit the label on the open side: below when the line falls into the point.
        const falling = pts[i - 1] && pts[i - 1][1] < p[1];
        const t = s("text", { class: "direct", x: p[0] - 7, y: falling ? p[1] + 16 : p[1] - 9, "text-anchor": "end" }, ctx.svg);
        t.textContent = fmt.pct(sr.value(state.runs[i]));
      }
    });
  }
  hitBands(ctx, (i) => {
    if (i == null) { cross.setAttribute("visibility", "hidden"); return; }
    cross.setAttribute("x1", ctx.xc(i));
    cross.setAttribute("x2", ctx.xc(i));
    cross.setAttribute("visibility", "visible");
    const run = state.runs[i];
    const rows = run.published
      ? present.map((sr) => [sr.color, fmt.pct(sr.value(run)), sr.short])
      : [[css("--none"), "No report", ""]];
    showTip(ctx, i, rows);
  }, (i) => `${shortDate(state.runs[i].date)}: ${present.map((sr) => `${sr.short} ${fmt.pct(sr.value(state.runs[i]))}`).join(", ")}`);
}

function renderCharts() {
  const root = document.getElementById("charts");
  root.replaceChildren();
  // Cost leads, full width: where the money goes is the most telling chart.
  stackedCost(root);
  lineChart(root, {
    title: "Source quality",
    sub: "Share of cited sites on the trusted list, and of claims with a primary source or two independent ones",
    series: [
      { label: "Cited sites on the trusted list", short: "trusted sites", color: css("--series-1"),
        value: (r) => (r.published ? score(r, "source_quality") : null) },
      { label: "Claims corroborated", short: "claims corroborated", color: css("--series-2"),
        value: (r) => (r.published ? score(r, "claims_corroborated") : null) },
    ],
  });
  columnChart(root, {
    title: "Sources cited per report", sub: "Distinct sources in each report's source list",
    value: (r) => score(r, "source_count"), format: fmt.int, yFormat: fmt.int,
  });
  stackedTopics(root);
  columnChart(root, {
    title: "Run time", sub: "Minutes from start to finish",
    value: (r) => r.duration_minutes, format: fmt.min, yFormat: (v) => String(Math.round(v)), withHeld: true,
  });
}

// --- table view ------------------------------------------------------------------

function renderTable() {
  const head = ["Week", "Status", "Topics", "Sources", "Trusted sites", "Claims corroborated", "Cost",
    ...STAGES.map(([, label]) => label), "Run time"];
  const rows = [...state.runs].reverse().map((r) => [
    shortDate(r.date),
    r.published ? "Published" : "No report",
    r.published ? String(r.topics.length) : "–",
    r.published ? fmt.int(score(r, "source_count")) : "–",
    r.published ? fmt.pct(score(r, "source_quality")) : "–",
    r.published ? fmt.pct(score(r, "claims_corroborated")) : "–",
    fmt.usd(r.cost_usd),
    ...STAGES.map(([k]) => {
      const split = stageSplit(r);
      return split ? fmt.usd(split[k]) : "–";
    }),
    fmt.min(r.duration_minutes),
  ]);
  document.getElementById("table").replaceChildren(
    h("thead", {}, h("tr", {}, ...head.map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, ...rows.map((row) => h("tr", {}, ...row.map((c) => h("td", {}, c))))));
}

// --- live status and motion ----------------------------------------------------------

// The weekly run is scheduled for Monday 05:00 UTC.
function nextRun(now = new Date()) {
  const t = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 5));
  t.setUTCDate(t.getUTCDate() + ((8 - t.getUTCDay()) % 7));
  if (t <= now) t.setUTCDate(t.getUTCDate() + 7);
  return t;
}

function until(t) {
  const mins = Math.max(0, Math.round((t - Date.now()) / 60000));
  const d = Math.floor(mins / 1440);
  const hr = Math.floor((mins % 1440) / 60);
  if (d > 0) return `${d} day${d === 1 ? "" : "s"}, ${hr} hr`;
  if (hr > 0) return `${hr} hr, ${mins % 60} min`;
  return `${mins} min`;
}

function renderStatus() {
  const el = document.getElementById("status");
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  const latest = latestReport();
  // Monday 05:00 to 08:00 UTC with no report for today yet: the run is going.
  const running = now.getUTCDay() === 1 && now.getUTCHours() >= 5 && now.getUTCHours() < 8
    && !state.byDate.has(today);
  if (running) {
    el.replaceChildren(h("span", { class: "pulse", "aria-hidden": "true" }),
      h("span", {}, "This week's run is in progress. The report appears here when it finishes."));
    return;
  }
  const next = nextRun(now);
  const day = next.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  el.replaceChildren(
    h("span", { class: "pulse", "aria-hidden": "true" }),
    h("span", {}, "Next run in ", h("strong", {}, until(next))),
    h("span", { class: "status-sub" }, `${day}, 05:00 UTC`),
    latest ? h("span", { class: "status-sub" }, `Last report ${age(latest.date)}`) : null);
}

// Count a tile's number up from zero, keeping its prefix and suffix ("$", " min").
function countUp(el) {
  const final = el.dataset.final || el.textContent;
  el.dataset.final = final;
  const m = final.match(/^([^0-9]*)([0-9][0-9,]*(?:\.[0-9]+)?)(.*)$/);
  if (!m) return;
  const [, pre, num, post] = m;
  const target = parseFloat(num.replace(/,/g, ""));
  const decimals = (num.split(".")[1] || "").length;
  const start = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - start) / 1100);
    el.textContent = pre + (target * (1 - (1 - p) ** 3)).toFixed(decimals) + post;
    if (p < 1) requestAnimationFrame(step);
    else el.textContent = final;
  };
  requestAnimationFrame(step);
}

// Fade elements up as they scroll into view, then run `onIn` once for each.
function reveal(nodes, onIn) {
  if (!MOTION) return;
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      e.target.classList.add("in");
      if (onIn) onIn(e.target);
      io.unobserve(e.target);
    }
  }, { threshold: 0, rootMargin: "0px 0px -6% 0px" });
  nodes.forEach((n, i) => {
    n.classList.add("reveal");
    n.style.setProperty("--d", `${Math.min(i, 6) * 60}ms`);
    io.observe(n);
  });
}

// Bars grow and lines draw the first time the charts come into view. Charts redraw
// on resize, so the animation is switched off once it has played.
function animateCharts() {
  if (!MOTION) return;
  const charts = document.getElementById("charts");
  charts.classList.add("pre");
  const io = new IntersectionObserver(([e]) => {
    if (!e.isIntersecting) return;
    io.disconnect();
    charts.classList.replace("pre", "animate");
    setTimeout(() => charts.classList.add("settled"), 2200);
  }, { threshold: 0.12 });
  io.observe(charts);
}

// --- start -------------------------------------------------------------------------

async function start() {
  const res = await fetch("data/index.json", { cache: "no-cache" });
  const index = await res.json();
  state.runs = [...index.runs].sort((a, b) => a.date.localeCompare(b.date));
  state.byDate = new Map(state.runs.map((r) => [r.date, r]));
  renderTiles();
  renderWeeks();
  renderCharts();
  renderTable();
  if (index.updated) {
    document.getElementById("updated").textContent = `Last updated ${longDate(index.updated.slice(0, 10))}.`;
  }
  const latest = latestReport() || state.runs[state.runs.length - 1];
  select(latest.date);
  renderStatus();
  setInterval(renderStatus, 60000);
  reveal([...document.querySelectorAll("#reports > .section-label, #reports > h2, #reports > .lede, #reports > .weeks, #reports > .card")]);
  reveal([...document.querySelectorAll("#analytics > .section-label, #analytics > h2, #analytics > .lede")]);
  reveal([...document.querySelectorAll(".tile")], (tile) => countUp(tile.querySelector(".value")));
  animateCharts();

  // Redraw on width changes only; a redraw changes the height, which must not loop.
  let pending;
  let lastWidth = 0;
  new ResizeObserver(([entry]) => {
    const w = Math.round(entry.contentRect.width);
    if (w === lastWidth) return;
    lastWidth = w;
    clearTimeout(pending);
    pending = setTimeout(renderCharts, 120);
  }).observe(document.getElementById("charts"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderCharts);
}

start().catch((err) => {
  document.getElementById("report").textContent = "The dashboard data could not be loaded.";
  console.error(err);
});
