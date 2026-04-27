# Root-Cause Analysis: Click-Wrong-Target & Fill-Wrong-Field

This document explains why the current recorder + resolver combination
sometimes clicks the wrong element or fills the wrong field at replay
time, and what the v2 rewrite changes to fix it.

## 1. Recorder weaknesses

### 1.1 Frame chain is lost
The current overlay JS only writes:

```js
frame: (window.top === window) ? "top" : (window.name || "frame")
```

— a flat string. There is no path back to the iframe at replay time, so
when a form lives inside an iframe (Facebook DMCA, Trademark, embed
widgets, …) the resolver runs `page.locator(selector)` against the
**top frame**, finds nothing, and skips the field — or worse, finds an
unrelated element with a matching selector in the top frame.

### 1.2 css-path is too lossy
`cssPath()` walks up only 6 ancestors and discards every id/class that
"looks random". On modern React/Vue apps virtually every class is
auto-generated (`css-1abc23d`), so the resulting path is often:

```
div > div > div > div > input:nth-of-type(2)
```

— matches dozens of inputs across the page. Recording two text
inputs in the same panel produces **identical** css-paths.

### 1.3 No element fingerprint at replay time
The resolver returns the first locator that matches a selector. It does
not verify that the matched element is the one originally recorded.
Consequence: if the page DOM shifts (an extra wrapper div, a sticky
header that adds a node, a sibling input added above), the selector
points at a *different* element and we silently fill the wrong field.

### 1.4 Selector ordering is hand-tuned, not weight-driven
Targets are tried in the order the JS happened to push them. There is no
notion of "this selector is more reliable than that one" — `data-testid`
should always beat `placeholder`, but the current code does not enforce
that.

### 1.5 Custom widgets are invisible to the recorder
The recorder only fires on `change`/`blur`/`click`-on-submit-shaped
elements. So:

- Custom dropdowns (`div[role=combobox] + ul[role=listbox]`) — the
  `change` event never fires; nothing is recorded.
- Custom radio groups (`div[role=radio]`) — neither `change` nor the
  submit-detector fires.
- Contenteditable rich text (Facebook description boxes) — no `value`
  attribute, captured as empty.
- React-select / Material UI Autocomplete — needs a click to open the
  list, then a click on the option; both clicks are silently dropped.

### 1.6 Value-only capture loses the action sequence
The recorder ships a single snapshot per field: the *final* `.value`.
On replay we have no idea whether that value arrived by:

- Typing it (must replay keystrokes for React)
- Pasting it (`navigator.clipboard.writeText` + Ctrl+V)
- Picking it from an autocomplete (need to click an option, not type)
- Selecting from a `<select>` (need `select_option`, not `fill`)
- Tabbing through to focus, then typing

So `loc.fill(value)` works for plain HTML inputs and many React inputs,
but fails on Autocomplete / combobox / contenteditable / file-drag-drop.

### 1.7 Submit detection is text-only
`looksLikeSubmit` checks the visible text. Two identical "Send" buttons
on the same page (one for newsletter, one for the report) collapse to
the same selector. Replay clicks the wrong one.

### 1.8 Multi-step forms collide on `field_id`
After `Next`, the overlay re-injects but `__af_anon_counter` resets and
fields with similar names (`name`, `name`) on different steps become
duplicates that the de-dup logic combines into one.

### 1.9 `blur` snapshots can capture stale state
When React rerenders on blur, the blur handler may execute *after* the
new render has cleared the field. The snapshot then captures `value=""`
and the field is recorded with an empty value.

## 2. Resolver weaknesses

### 2.1 `.first` everywhere
Every strategy collapses to `.first` without disambiguation:

```python
loc = page.locator(selector).first
```

If the selector matches three elements on the page, the resolver
silently picks the first one in document order — frequently not what
the user clicked.

### 2.2 No iframe traversal
`page.locator()` does not descend into iframes by default. There is no
`frame_locator` walk, so any field inside an iframe is unreachable.

### 2.3 No shadow-DOM handling
Chromium-only `>>>` shadow piercing is not used; web components are
unreachable.

### 2.4 `nearby_text` xpath is too greedy
`xpath=//*[contains(., "Email")]` matches every ancestor that contains
the substring "Email" anywhere in its subtree — frequently the
`<body>` itself. Then `following::input` returns the very first input
in the document.

### 2.5 No verification of the picked element
After resolving, we never check the picked element against the recorded
fingerprint (tag, type, role, accessible name, neighbours). A
mis-resolution becomes a silent wrong-fill.

## 3. Fill-engine weaknesses

### 3.1 One method for every field
`loc.fill(value)` is used for text/email/textarea/etc. It works on most
inputs but not on:

- React inputs that listen for `keydown` (rare but real)
- Custom autocomplete that opens a dropdown on each keystroke
- Contenteditable rich text
- Stripe / Adyen card iframes (cross-origin — needs a different path)

### 3.2 Custom radio / checkbox groups
`loc.check()` requires a real `<input type=checkbox>`. FB-style
`div[role=checkbox]` needs `loc.click()` and a verification of
`aria-checked`.

### 3.3 No retry on transient instability
If the element is mid-animation when we click, Playwright raises
`element is not stable`. The current engine retries the *resolution*
3× but never the *action*, so a flaky animation → permanent skip.

## 4. What v2 fixes

The v2 modules in this rewrite address every item above:

| File                       | Fixes                                           |
|----------------------------|-------------------------------------------------|
| `element_fingerprint.py`   | 1.3, 1.4, 2.1, 2.5                              |
| `recorder_v2.py`           | 1.1, 1.2, 1.5, 1.6, 1.7, 1.8, 1.9               |
| `resolver_v2.py`           | 2.1, 2.2, 2.3, 2.4, 2.5                         |
| `replay_engine.py`         | 1.6, 3.1, 3.2, 3.3                              |
| `worker_pool.py`           | New: multi-account concurrent execution         |

## 5. Concrete behaviour changes you will see

After the rewrite:

- The recorder captures the **frame chain** so iframe-based forms are
  resolved correctly on replay.
- Every field in the config carries a **fingerprint** (tag + role +
  accessible name + attributes + neighbouring text); the resolver
  rejects matches whose fingerprint differs from the recording, and
  moves on to the next strategy.
- Selector strategies are tried in **weighted priority order**:
  `data-testid > stable-id > role+name > aria-label > label-for >
  label-text > attribute-css-path > positional`.
- Each recorded action stores the **input method** that was actually
  used (type / paste / select_option / click / check / set_files), and
  the replay engine replays the same method — so React/Vue/custom-widget
  fields fill correctly.
- Custom radio / checkbox / combobox elements (`div[role=…]`) are
  detected and replayed via `click + aria-checked verification`.
- Submit clicks now carry the same fingerprint + selectors as fields,
  so two "Send" buttons on the same page no longer collide.
- Multi-account: the `worker_pool` runs N accounts concurrently, each
  with its own persistent `BrowserContext`, proxy, and cookie jar; tasks
  are pulled from a shared queue with retry-on-fail.
