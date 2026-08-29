// Runs the real staged-file shelf out of app/static/app.js against a stub DOM
// and prints what it did, as JSON.
//
// Wes, 2026-08-28: "I still can't modify (delete) files that I have included in
// a note before sending it. I want to be able to view what I have included,
// rename it, or remove it."
//
// Everything here is about a FileList, which is the one object in the DOM you
// cannot edit: there is no remove, no rename, and `name` is read-only. Every
// operation is a full rebuild through a DataTransfer, so the things that can go
// wrong are all about what the rebuild does to the OTHER files - dropping them,
// reordering them, or losing the bytes. None of that is visible in the source
// text, so the real functions are driven against a real (stub) input.
//
// Called by tests/test_attach_shelf.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = appjs.indexOf(to, start);
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

// From the FileList helpers through the shelf renderer, in file order - the
// whole "what is staged on this note" mechanism and nothing else.
const SRC = slice(
  "// FileList is read-only, so adding to it means rebuilding",
  "function initDropzones() {",
  "attachment shelf"
);

// --- the stub world --------------------------------------------------------

// A File that is honest about the two things this code relies on: the bytes are
// carried through a rename (`new File([f], name, ...)` must copy them), and the
// identity of an untouched file is preserved by a rebuild.
class StubFile {
  constructor(parts, name, opts) {
    const o = opts || {};
    this.parts = parts;
    this.name = name;
    this.type = o.type || "";
    this.lastModified = o.lastModified === undefined ? 1000 : o.lastModified;
    this.size = o.size === undefined ? bytesOf(parts) : o.size;
  }
}

function bytesOf(parts) {
  return parts.reduce((n, p) => n + (p instanceof StubFile ? p.size : String(p).length), 0);
}

globalThis.File = StubFile;
globalThis.DataTransfer = class {
  constructor() {
    const files = [];
    this.files = files;
    this.items = { add: (f) => files.push(f) };
  }
};

// The smallest DOM the shelf actually touches.
function makeEl(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    children: [],
    attrs: {},
    classes: [],
    hidden: false,
    value: "",
    listeners: {},
    parentNode: null,
  };
  el.classList = {
    add: (c) => { if (el.classes.indexOf(c) < 0) el.classes.push(c); },
    remove: (c) => { const i = el.classes.indexOf(c); if (i >= 0) el.classes.splice(i, 1); },
    contains: (c) => el.classes.indexOf(c) >= 0,
    toggle: (c, on) => { if (on) el.classList.add(c); else el.classList.remove(c); },
  };
  Object.defineProperty(el, "className", {
    get: () => el.classes.join(" "),
    set: (v) => { el.classes = v ? v.split(" ") : []; },
  });
  // Assigning textContent REPLACES every child, which is how the renderer
  // empties the shelf before redrawing it. A stub that only stored the string
  // would leave the old rows in place, and the redraw would read as appending
  // - which is a bug the real DOM does not have and this harness must not
  // invent.
  let text = "";
  Object.defineProperty(el, "textContent", {
    get: () => text,
    set: (v) => {
      text = String(v);
      el.children.forEach((c) => { c.parentNode = null; });
      el.children.length = 0;
    },
  });
  el.appendChild = (child) => { child.parentNode = el; el.children.push(child); return child; };
  el.insertAdjacentElement = (where, node) => {
    const parent = el.parentNode;
    if (!parent) return node;
    const at = parent.children.indexOf(el);
    parent.children.splice(where === "afterend" ? at + 1 : at, 0, node);
    node.parentNode = parent;
    return node;
  };
  el.remove = () => {
    if (!el.parentNode) return;
    const at = el.parentNode.children.indexOf(el);
    if (at >= 0) el.parentNode.children.splice(at, 1);
    el.parentNode = null;
  };
  el.getAttribute = (n) => (n in el.attrs ? el.attrs[n] : null);
  el.setAttribute = (n, v) => { el.attrs[n] = String(v); };
  el.addEventListener = (type, fn) => {
    if (!el.listeners[type]) el.listeners[type] = [];
    el.listeners[type].push(fn);
  };
  el.fire = (type, ev) => (el.listeners[type] || []).forEach((fn) => fn(ev || {}));
  el.focus = () => { world.focused = el; };
  el.setSelectionRange = (a, b) => { el.selection = [a, b]; };
  el.querySelector = (sel) => find(el, sel);
  el.querySelectorAll = (sel) => findAll(el, sel);
  return el;
}

function matchesSel(el, sel) {
  if (sel.charAt(0) === ".") return el.classList.contains(sel.slice(1));
  return el.tagName === sel.toUpperCase();
}

function findAll(root, sel) {
  const hits = [];
  (function walk(node) {
    node.children.forEach((c) => { if (matchesSel(c, sel)) hits.push(c); walk(c); });
  })(root);
  return hits;
}

function find(root, sel) {
  return findAll(root, sel)[0] || null;
}

let world;

function makeInput(files, maxBytes) {
  const input = makeEl("input");
  input.files = files;
  if (maxBytes) input.setAttribute("data-max-bytes", maxBytes);
  return input;
}

function build() {
  world = { focused: null, revoked: [], created: 0 };
  globalThis.document = { createElement: makeEl };
  globalThis.window = { URL: true };
  globalThis.URL = {
    createObjectURL: () => { world.created += 1; return "blob:stub/" + world.created; },
    revokeObjectURL: (u) => { world.revoked.push(u); },
  };
  // eslint-disable-next-line no-new-func
  return new Function(SRC + "; return { renderAttachShelf, removeFile, renameFile, addFiles, recordedNames, fileSize };")();
}

// What a rendered shelf says, in the order the rows are in.
function readShelf(shelf) {
  return shelf.children.map((row) => ({
    name: find(row, ".attach-row-name").textContent,
    size: find(row, ".attach-row-size").textContent,
    oversize: row.classList.contains("oversize"),
    hasThumb: !!find(row, "IMG"),
    ext: find(row, ".attach-row-ext") ? find(row, ".attach-row-ext").textContent : null,
    buttons: findAll(row, "BUTTON").map((b) => b.textContent),
  }));
}

function buttonNamed(row, label) {
  return findAll(row, "BUTTON").filter((b) => b.textContent === label)[0];
}

const out = {};

const png = (name) => new StubFile(["imagebytes"], name, { type: "image/png", size: 2048 });
const pdf = (name) => new StubFile(["pdfbytes"], name, { type: "application/pdf", size: 5000 });

// 1. What the shelf shows: a thumbnail for a picture, an extension for
//    everything else, and the three controls on every row.
{
  const api = build();
  const input = makeInput([png("shot.png"), pdf("statement.pdf")]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  out.rendered = readShelf(shelf);
}

// 2. Remove takes exactly one file off, leaving the others in their order.
//    Rebuilding a FileList is all-or-nothing, so the failure mode here is
//    losing the files either side of the one that was removed.
{
  const api = build();
  const input = makeInput([png("a.png"), png("b.png"), png("c.png")]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  buttonNamed(shelf.children[1], "remove").fire("click");
  out.removed = {
    left: input.files.map((f) => f.name),
    shelf: readShelf(shelf).map((r) => r.name),
  };
}

// 3. Rename rewrites the file that will actually be posted - name changed,
//    bytes and type and position all unchanged.
{
  const api = build();
  const input = makeInput([png("a.png"), png("IMG_0838.png"), png("c.png")]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  buttonNamed(shelf.children[1], "rename").fire("click");
  const field = find(shelf.children[1], ".attach-row-rename");
  field.value = "the todo list on my phone.png";
  field.fire("keydown", { key: "Enter", preventDefault() {} });
  out.renamed = {
    names: input.files.map((f) => f.name),
    // Same bytes, same type, same lastModified: a rename must not be a
    // different file.
    size: input.files[1].size,
    type: input.files[1].type,
    lastModified: input.files[1].lastModified,
    shelf: readShelf(shelf).map((r) => r.name),
  };
}

// 4. Escape abandons a rename. The field goes away and the file is untouched.
{
  const api = build();
  const input = makeInput([png("keep-me.png")]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  buttonNamed(shelf.children[0], "rename").fire("click");
  const field = find(shelf.children[0], ".attach-row-rename");
  field.value = "something else.png";
  let stopped = false;
  field.fire("keydown", {
    key: "Escape",
    preventDefault() {},
    stopPropagation() { stopped = true; },
  });
  out.escaped = {
    names: input.files.map((f) => f.name),
    fieldGone: !find(shelf.children[0], ".attach-row-rename"),
    nameVisible: !find(shelf.children[0], ".attach-row-name").hidden,
    // Stopped, or the page-wide Escape handler also reads this as "close
    // whatever is open" and shuts the section out from under the rename.
    stoppedPropagation: stopped,
  };
}

// 5. A rename to a name another staged file already has is refused. The server
//    stores by name, so two files claiming one name is a silent overwrite.
{
  const api = build();
  const input = makeInput([png("a.png"), png("b.png")]);
  out.duplicate = {
    result: api.renameFile(input, "b.png", "a.png"),
    names: input.files.map((f) => f.name),
  };
}

// 6. An empty name, and a rename to the name it already has, are both refused
//    rather than producing a file called "".
{
  const api = build();
  const input = makeInput([png("a.png")]);
  out.emptyName = {
    blank: api.renameFile(input, "a.png", "   "),
    same: api.renameFile(input, "a.png", "a.png"),
    names: input.files.map((f) => f.name),
  };
}

// 7. A voice memo is left to the recorder's own shelf. Listed here too it would
//    have two rows and two delete buttons for one file.
{
  const api = build();
  const memo = new StubFile(["audio"], "voice-memo-2026-08-28.webm", { type: "audio/webm" });
  const input = makeInput([png("shot.png"), memo]);
  api.recordedNames(input)[memo.name] = true;
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  out.recordingsExcluded = {
    shelf: readShelf(shelf).map((r) => r.name),
    // Still going to be posted - it is excluded from the LIST, not from the
    // upload.
    posted: input.files.map((f) => f.name),
  };
}

// 8. A file the user happens to name like a recording is NOT hidden. The claim
//    is a registration by the recorder, not a guess from the name - hiding it
//    would take it off the only list that can remove it.
{
  const api = build();
  const input = makeInput([new StubFile(["x"], "voice-memo-notes.webm", { type: "audio/webm" })]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  out.namesakeNotHidden = readShelf(shelf).map((r) => r.name);
}

// 9. A file over the size limit says so on its own row, where the file is,
//    rather than only in a status line somewhere else on the form.
{
  const api = build();
  const input = makeInput([png("small.png"), new StubFile(["x"], "huge.mov", { type: "video/quicktime", size: 99999999 })], 1000000);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  out.oversize = readShelf(shelf).map((r) => ({ name: r.name, oversize: r.oversize, size: r.size }));
}

// 10. Redrawing revokes the object URLs of the rows it replaced. Dropping ten
//     screenshots and changing your mind about them would otherwise pin all ten
//     in memory for as long as the page is open.
{
  const api = build();
  const input = makeInput([png("a.png"), png("b.png")]);
  const shelf = makeEl("div");
  api.renderAttachShelf(input, shelf);
  const first = world.created;
  api.renderAttachShelf(input, shelf);
  out.objectUrls = {
    createdOnFirstDraw: first,
    revokedOnRedraw: world.revoked.length,
    rowsAfterRedraw: shelf.children.length,
  };
}

// 11. Sizes read as sizes.
{
  const api = build();
  out.sizes = [api.fileSize(512), api.fileSize(2048), api.fileSize(5 * 1024 * 1024)];
}

console.log(JSON.stringify(out, null, 2));
