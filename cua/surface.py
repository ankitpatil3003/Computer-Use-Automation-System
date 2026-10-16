"""Surface: how we perceive and act on an application.

This is the seam between "how we see/drive a UI" and "the recorded flow". Nothing in schema.py
or the replay engine knows about Playwright; they speak Locator/Check/Step. A legacy-web surface
is this same class; a desktop surface (Windows UIA / macOS AX tree) would implement the same
protocol with the same role/name/text-anchor strategies. See REPORT.md, Heterogeneity.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

from playwright.sync_api import Error as PWError, Frame, Locator as PWLocator, sync_playwright

from cua.handoff import ControlError, Lease
from cua.schema import Check, CssStrategy, Locator, RoleStrategy, Strategy, TableCellStrategy, TextAnchorStrategy


class LocatorError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class Surface(Protocol):
    url: str
    lease: Lease | None  # who may act; perform() refuses unless it says AUTOMATION
    human_events: list[dict]  # actions captured while a human holds the lease

    def idle(self, ms: int) -> None: ...

    def goto(self, url: str) -> None: ...
    def reset_session(self) -> None: ...
    def observe(self) -> str: ...
    def locator_for(self, ref: str) -> Locator: ...
    def cell_locator(self, frame: list[str], row: str, column: str | None) -> Locator: ...
    def perform(self, action: str, target: Locator | None, value: str | None, timeout_ms: int) -> int: ...
    def holds(self, check: Check) -> bool: ...
    def wait_for(self, check: Check, timeout_ms: int) -> bool: ...
    def read(self, target: Locator, timeout_ms: int) -> str: ...
    def screenshot(self, path: str | Path) -> None: ...


# Approximate role/name in the page. Only used to *propose* strategies at record time; every
# proposal is then verified against Playwright's real accessibility engine before it is kept.
_ROLE_NAME_JS = """
const roleOf = el => {
  const t = el.tagName, ty = (el.type || '').toLowerCase();
  if (el.getAttribute('role')) return el.getAttribute('role');
  if (t === 'A' && el.hasAttribute('href')) return 'link';
  if (t === 'BUTTON' || (t === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(ty))) return 'button';
  if (t === 'INPUT' && ty === 'checkbox') return 'checkbox';
  if (t === 'INPUT' && ty === 'radio') return 'radio';
  if ((t === 'INPUT' && ty !== 'hidden') || t === 'TEXTAREA') return 'textbox';
  if (t === 'SELECT') return 'combobox';
  return null;
};
const nameOf = el => (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
  || (['submit', 'button', 'reset'].includes((el.type || '').toLowerCase()) ? el.value : '')
  || (['A', 'BUTTON'].includes(el.tagName) ? el.innerText : '') || el.title || el.placeholder || '').trim();
"""

# Render a frame as a human would read it: text in reading order, one line per row/block,
# interactive controls inlined as [eN role "name"]. No CSS, no markup — works the same on a
# clean SPA or a 1998 frameset, and maps 1:1 onto an OS accessibility tree.
_OBSERVE_JS = "(start) => {" + _ROLE_NAME_JS + """
  const refs = [], out = []; let line = [];
  const flush = () => { const s = line.join(' ').replace(/\\s+/g, ' ').trim(); if (s) out.push(s); line = []; };
  const BLOCK = new Set(['TR', 'P', 'DIV', 'BR', 'TABLE', 'FORM', 'H1', 'H2', 'H3', 'H4', 'LI', 'UL', 'HR', 'CENTER']);
  const visible = el => el.checkVisibility ? el.checkVisibility({checkVisibilityCSS: true}) : el.getClientRects().length > 0;
  const walk = node => {
    if (node.nodeType === 3) { const t = node.textContent.trim(); if (t) line.push(t); return; }
    if (node.nodeType !== 1 || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(node.tagName) || !visible(node)) return;
    const role = roleOf(node);
    if (role) {
      let s = `[e${start + refs.length} ${role} "${nameOf(node)}"`;
      if (node.type === 'password') s += ' password';
      else if (role === 'textbox' && node.value) s += ` value="${node.value}"`;
      refs.push(node); line.push(s + ']');
      return;
    }
    const block = BLOCK.has(node.tagName);
    if (block) flush();
    for (const c of node.childNodes) walk(c);
    if (node.tagName === 'TD' || node.tagName === 'TH') line.push('|');
    if (block) flush();
  };
  if (document.body) walk(document.body);
  flush();
  window.__cuaRefs = refs;
  return {text: out.join('\\n'), count: refs.length};
}"""

_DESCRIBE_JS = "(el) => {" + _ROLE_NAME_JS + """
  let anchor = null;
  const tr = el.closest('tr');
  if (tr) for (const c of tr.cells) {
    if (c.contains(el)) continue;
    const s = c.innerText.trim();
    if (s) { anchor = s; break; }
  }
  const doc = el.ownerDocument, ty = (el.type || '').toLowerCase();
  let css = el.tagName.toLowerCase();
  if (el.getAttribute('name')) css += `[name=${JSON.stringify(el.getAttribute('name'))}]`;
  if (['submit', 'button'].includes(ty) && el.value) css += `[value=${JSON.stringify(el.value)}]`;
  if (doc.querySelectorAll(css).length !== 1) {
    const parts = [];
    for (let n = el; n && n !== doc.body; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    css = 'body > ' + parts.join(' > ');
  }
  return {role: roleOf(el), name: nameOf(el), anchor, css};
}"""

# Installed in every frame of every document. Reports what a human operator does to the live
# session: which control (role/name, or its row label), never markup. Password values never
# leave the page; everything else is redacted by the run's redactor before it is persisted.
_CAPTURE_JS = "(() => { if (window.__cuaCapture) return; window.__cuaCapture = true;" + _ROLE_NAME_JS + """
  const label = el => {
    const tr = el.closest('tr');
    if (tr) for (const c of tr.cells) { if (!c.contains(el) && c.innerText.trim()) return c.innerText.trim(); }
    return '';
  };
  const post = (kind, target) => {
    const el = target.closest && target.closest('a,button,input,select,textarea');
    const role = el && roleOf(el);
    if (!role || !window.__cuaHuman || (kind === 'click' && !['link', 'button', 'checkbox', 'radio'].includes(role))) return;
    const ev = {kind, role, name: nameOf(el) || label(el)};
    if (kind === 'change') ev.value = (el.type || '').toLowerCase() === 'password' ? '[SECRET]' : el.value;
    window.__cuaHuman(ev);
  };
  document.addEventListener('click', e => post('click', e.target), true);
  document.addEventListener('change', e => post('change', e.target), true);
})()"""

_CELL = "*[self::td or self::th]"


def _xq(s: str) -> str:
    """Quote a string as an XPath 1.0 literal."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat('" + s.replace("'", "', \"'\", '") + "')"


def _frame_path(frame: Frame) -> list[str] | None:
    """Frame names from the top document; None if any frame on the way is unnamed."""
    path = []
    while frame.parent_frame is not None:
        if not frame.name:
            return None  # ponytail: named frames only; add url-pattern frame matching for anonymous iframes
        path.insert(0, frame.name)
        frame = frame.parent_frame
    return path


class PlaywrightSurface:
    def __init__(self, headless: bool = True, allow_url: Callable[[str], bool] | None = None):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=headless)
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 800})
        self._refs: dict[str, tuple[Frame, int]] = {}
        self.blocked: list[str] = []
        self.lease: Lease | None = None
        self.human_events: list[dict] = []
        self.page.context.expose_binding("__cuaHuman", self._on_human)
        self.page.context.add_init_script(_CAPTURE_JS)
        if allow_url:
            # Enforced at the network layer, so a click that would navigate (any frame) off the
            # allowlist is stopped before the request leaves the browser, not detected afterwards.
            def guard(route):
                if allow_url(route.request.url):
                    route.continue_()
                else:
                    self.blocked.append(route.request.url)
                    route.abort("blockedbyclient")
            self.page.context.route("**/*", guard)

    def __enter__(self) -> PlaywrightSurface:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()

    @property
    def url(self) -> str:
        return self.page.url

    def goto(self, url: str) -> None:
        self._require_control()
        self.page.goto(url, wait_until="domcontentloaded")

    def _require_control(self) -> None:
        if self.lease is not None and (state := self.lease.state) != "AUTOMATION":
            raise ControlError(f"automation does not hold the session (lease state {state})")

    def _on_human(self, source, ev: dict) -> None:
        # Automation's own clicks fire the same listener; only the lease says whose hands they were.
        if self.lease is not None and self.lease.state == "HUMAN":
            path = _frame_path(source["frame"])
            self.human_events.append({"t": time.strftime("%H:%M:%S"), "frame": "/".join(path) if path else "top", **ev})

    def idle(self, ms: int) -> None:
        """Wait while still servicing the browser (human-action callbacks run during this)."""
        self.page.wait_for_timeout(ms)

    def reset_session(self) -> None:
        """Drop the app session (cookies) so the next goto starts from a known state."""
        self.page.context.clear_cookies()

    # --- perception (discovery) ---------------------------------------------------------------

    def observe(self) -> str:
        self._refs.clear()
        parts, n = [], 0
        for frame in self.page.frames:
            try:
                res = frame.evaluate(_OBSERVE_JS, n)
            except PWError:
                continue  # frame navigating/detached; next observe will catch it
            for i in range(res["count"]):
                self._refs[f"e{n + i}"] = (frame, i)
            n += res["count"]
            if res["text"]:
                path = _frame_path(frame)
                label = "/".join(path) if path else ("top" if path == [] else "(unnamed)")
                parts.append(f"--- frame: {label}\n{res['text']}")
        return "\n".join(parts)

    def locator_for(self, ref: str) -> Locator:
        """Turn an observed control into a multi-strategy locator, keeping only strategies
        verified to match exactly this element on the live page."""
        if ref not in self._refs:
            raise LocatorError("locator_not_found", f"unknown ref {ref!r}; observe again")
        frame, idx = self._refs[ref]
        path = _frame_path(frame)
        if path is None:
            raise LocatorError("locator_not_found", "control is inside an unnamed frame")
        try:
            el = frame.evaluate_handle("i => window.__cuaRefs[i]", idx).as_element()
            info = el.evaluate(_DESCRIBE_JS)
        except PWError as e:
            raise LocatorError("locator_not_found", f"ref {ref} is stale ({e.message.splitlines()[0]}); observe again")
        proposals: list[Strategy] = []
        if info["role"] and info["name"]:
            proposals.append(RoleStrategy(role=info["role"], name=info["name"]))
        if info["role"] and info["anchor"]:
            proposals.append(TextAnchorStrategy(anchor=info["anchor"], role=info["role"]))
        proposals.append(CssStrategy(selector=info["css"]))
        return self._verified(frame, path, el, proposals, f'{info["role"]} "{info["name"] or info["anchor"] or ""}"')

    def cell_locator(self, frame: list[str], row: str, column: str | None) -> Locator:
        """Locator for a data cell (an output to extract), verified unique on the live page."""
        lead = TableCellStrategy(row=row, column=column)
        f = self._frame(frame)
        loc = self._loc(f, lead)
        if loc.count() != 1:
            raise LocatorError("locator_ambiguous" if loc.count() else "locator_not_found",
                               f"cell row={row!r} column={column!r} matched {loc.count()}")
        el = loc.element_handle()
        css = CssStrategy(selector=el.evaluate(_DESCRIBE_JS)["css"])
        return self._verified(f, frame, el, [lead, css], f"cell {row} / {column or 'value'}")

    def _verified(self, frame: Frame, path: list[str], el, proposals: list[Strategy], desc: str) -> Locator:
        kept = []
        for s in proposals:
            loc = self._loc(frame, s)
            if loc.count() == 1 and loc.evaluate("(n, t) => n === t", el):
                kept.append(s)
        if not kept:
            raise LocatorError("locator_ambiguous", f"no strategy identifies {desc} uniquely")
        return Locator(frame=path, strategies=kept, description=desc)

    # --- targeting (shared by discovery and replay) --------------------------------------------

    def _frame(self, path: list[str]) -> Frame:
        frame = self.page.main_frame
        for name in path:
            # child_frames can still list frames detached by an earlier navigation; skip them
            frame = next((f for f in frame.child_frames if f.name == name and not f.is_detached()), None)
            if frame is None:
                raise LocatorError("locator_not_found", f"frame {'/'.join(path)!r} not present")
        return frame

    def _loc(self, frame: Frame, s: Strategy) -> PWLocator:
        if isinstance(s, RoleStrategy):
            loc = frame.get_by_role(s.role, name=s.name, exact=True)
        elif isinstance(s, TextAnchorStrategy):
            loc = frame.locator(f"xpath=//tr[{_CELL}[normalize-space(.)={_xq(s.anchor)}]]").get_by_role(s.role)
        elif isinstance(s, TableCellStrategy):
            row = f"//tr[{_CELL}[1][normalize-space(.)={_xq(s.row)}]]"
            if s.column is None:
                xp = f"{row}/{_CELL}[1]/following-sibling::{_CELL}[1]"
            else:
                col = (f"count(ancestor::table[1]//tr[{_CELL}[normalize-space(.)={_xq(s.column)}]][1]"
                       f"/{_CELL}[normalize-space(.)={_xq(s.column)}]/preceding-sibling::{_CELL}) + 1")
                xp = f"{row}/{_CELL}[position() = {col}]"
            loc = frame.locator(f"xpath={xp}")
        else:
            loc = frame.locator(s.selector)
        return loc.filter(visible=True)

    def resolve(self, target: Locator, timeout_ms: int) -> tuple[PWLocator, int]:
        """Poll until a strategy matches exactly one visible element, in rank order.
        Returns (locator, index of the strategy that won) — index > 0 is a drift signal.
        Never picks "the first of several": ambiguity at the deadline is a hard failure."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            seen = "nothing matched"
            ambiguous = False
            try:
                frame = self._frame(target.frame)
                for i, s in enumerate(target.strategies):
                    n = self._loc(frame, s).count()
                    if n == 1:
                        return self._loc(frame, s), i
                    if n > 1 and not ambiguous:
                        ambiguous, seen = True, f"{s.by} matched {n} elements"
            except (LocatorError, PWError) as e:
                seen = getattr(e, "detail", None) or str(e).splitlines()[0]
            if time.monotonic() >= deadline:
                raise LocatorError("locator_ambiguous" if ambiguous else "locator_not_found",
                                   f"{target.description or target.strategies[0]}: {seen}")
            self.page.wait_for_timeout(100)

    def perform(self, action: str, target: Locator | None, value: str | None, timeout_ms: int) -> int:
        """Execute one action. Returns the winning strategy index (0 for goto)."""
        self._require_control()
        if action == "goto":
            self.goto(value)
            return 0
        loc, idx = self.resolve(target, timeout_ms)
        if action == "click":
            loc.click(timeout=timeout_ms)
        elif action == "fill":
            loc.fill(value, timeout=timeout_ms)
        elif action == "select":
            loc.select_option(value, timeout=timeout_ms)
        elif action == "press":
            loc.press(value, timeout=timeout_ms)
        else:
            raise ValueError(f"unknown action {action!r}")
        return idx

    # --- state -----------------------------------------------------------------------------------

    def _text(self, frame: Frame) -> str:
        try:
            return frame.evaluate("() => document.body ? document.body.innerText : ''")
        except PWError:
            return ""

    def holds(self, check: Check) -> bool:
        """Evaluate a check once, without waiting."""
        if check.text is not None:
            if check.frame is None:
                frames = self.page.frames
            else:
                try:
                    frames = [self._frame(check.frame)]
                except LocatorError:
                    return False
            if not any(check.text in self._text(f) for f in frames):
                return False
        if check.element is not None:
            try:
                self.resolve(check.element, 0)
            except LocatorError:
                return False
        return True

    def wait_for(self, check: Check, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while not self.holds(check):
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(100)
        return True

    def read(self, target: Locator, timeout_ms: int) -> str:
        loc, _ = self.resolve(target, timeout_ms)
        return loc.inner_text().strip()

    def screenshot(self, path: str | Path) -> None:
        self.page.screenshot(path=str(path), full_page=True)
