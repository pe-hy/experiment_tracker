// A DOM small enough to hand-write and big enough to render the whole app.
//
// The site has no build step and no test framework, so this stub is what lets the
// real view code run under plain node. It implements only what app.js touches; if a
// view starts using something new, this file fails loudly rather than silently
// returning undefined.

export class Node {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
  }

  get children() {
    return this.childNodes.filter(n => n instanceof Element);
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  append(...nodes) {
    nodes.forEach(n => this.appendChild(
      n instanceof Node ? n : new Text(String(n))));
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) this.childNodes.splice(i, 1);
    node.parentNode = null;
    return node;
  }

  replaceWith(node) {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i >= 0) this.parentNode.childNodes[i] = node;
    node.parentNode = this.parentNode;
  }

  get textContent() {
    return this.childNodes.map(n => n.textContent).join('');
  }

  set textContent(value) {
    this.childNodes = [];
    if (value !== '') this.appendChild(new Text(String(value)));
  }
}

export class Text extends Node {
  constructor(data) { super(); this.data = data; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

export class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.className = '';
    this.value = '';   // inputs and textareas read this before anything is typed
    this._innerHTML = '';
    this.classList = {
      toggle: (name, on) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) set.add(name); else set.delete(name);
        this.className = [...set].join(' ');
      },
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => this.classList.toggle(name, true),
    };
  }

  setAttribute(k, v) {
    this.attributes[k] = String(v);
    // Browsers keep these in sync; SVG elements are built via setAttribute('class').
    if (k === 'class') this.className = String(v);
  }
  scrollIntoView() {}
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  hasAttribute(k) { return k in this.attributes; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  focus() {}
  select() {}

  // Not parsed — recorded, so a test can assert on it and so that any accidental
  // use with untrusted data is visible rather than silent.
  set innerHTML(v) { this._innerHTML = String(v); }
  get innerHTML() { return this._innerHTML; }

  dispatch(type) { (this.listeners[type] || []).forEach(fn => fn({ preventDefault() {} })); }

  /** Depth-first search by predicate — enough for assertions. */
  find(predicate) {
    for (const child of this.childNodes) {
      if (child instanceof Element) {
        if (predicate(child)) return child;
        const hit = child.find(predicate);
        if (hit) return hit;
      }
    }
    return null;
  }

  findAll(predicate, out = []) {
    for (const child of this.childNodes) {
      if (child instanceof Element) {
        if (predicate(child)) out.push(child);
        child.findAll(predicate, out);
      }
    }
    return out;
  }
}

export function installDOM({ fetchImpl, hash = '' }) {
  const root = new Element('html');
  const body = new Element('body');
  const app = new Element('div');
  app.setAttribute('id', 'app');
  const builtAt = new Element('span');
  builtAt.setAttribute('id', 'built-at');
  const toggle = new Element('button');
  toggle.setAttribute('id', 'theme-toggle');
  body.append(app, builtAt, toggle);
  root.append(body);

  const byId = { app, 'built-at': builtAt, 'theme-toggle': toggle };

  globalThis.Node = Node;
  globalThis.document = {
    documentElement: root,
    body,
    createElement: tag => new Element(tag),
    createElementNS: (ns, tag) => new Element(tag),
    createTextNode: data => new Text(data),
    querySelector: sel => (sel.startsWith('#') ? byId[sel.slice(1)] || null : null),
  };
  globalThis.location = { hash };
  globalThis.window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    scrollTo() {},
    isSecureContext: false,
    history: { scrollRestoration: 'auto' },
  };
  globalThis.requestAnimationFrame = (fn) => setImmediate(fn);
  globalThis.localStorage = {
    _v: {},
    getItem(k) { return this._v[k] ?? null; },
    setItem(k, v) { this._v[k] = String(v); },
  };
  // `navigator` is a getter-only global in modern node, so it has to be redefined
  // rather than assigned.
  Object.defineProperty(globalThis, 'navigator', {
    value: {}, configurable: true, writable: true,
  });
  globalThis.fetch = fetchImpl;
  return { root, app, builtAt, toggle };
}
