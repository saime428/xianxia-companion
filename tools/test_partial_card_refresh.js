// Run with node tools/test_partial_card_refresh.js; no browser or game account needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/assets/static/app.js'), 'utf8');
class Element {}
class Form extends Element {
    constructor() {
        super();
        this.action = { name: 'action' }; // Named controls shadow HTMLFormElement.action.
        this.dataset = {};
    }
    matches() { return true; }
    getAttribute(name) { return name === 'action' ? '/runtime/estate/resources/action' : 'post'; }
}
const timers = [], calls = [], feedback = { textContent: '' };
let submit, panel;
const card = Object.assign(new Element(), {
    dataset: { partialRefreshCard: 'estate-resources' }, isConnected: true,
    addEventListener: (_name, callback) => { submit = callback; },
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    getBoundingClientRect: () => ({ top: 0 }),
    querySelector: selector => selector === '[data-estate-resource-status]' ? panel : null,
});
function status(pending) {
    return Object.assign(new Element(), {
        dataset: { pending, profileId: '2' }, isConnected: true,
        closest: () => card, querySelector: () => feedback,
        replaceWith(next) { this.isConnected = false; panel = next; },
    });
}
panel = status('0');
let incoming = status('1');
const nextCard = Object.assign(new Element(), { querySelector: () => incoming });
const context = {
    HTMLElement: Element, HTMLFormElement: Form,
    FormData: class {
        constructor(_form, submitter) { this.action = submitter?.value; }
    },
    document: { hidden: false, getElementById: () => null },
    window: { location: { href: '/modules/estate' }, setTimeout: callback => timers.push(callback) },
    DOMParser: class {
        parseFromString() { return { querySelector: selector => selector.startsWith('[data-partial') ? nextCard : incoming }; }
    },
    showGlobalLoading: async () => {}, hideGlobalLoading() {},
    fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, text: async () => '' }; },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function mountPartialCardRefresh(card)'), source.indexOf('function mountPartialCardRefreshes(')), context);

(async () => {
    context.mountPartialCardRefresh(card);
    await submit({ target: new Form(), submitter: { value: 'chest_status' }, preventDefault() {} });
    assert.equal(calls[0].url, '/runtime/estate/resources/action');
    assert.equal(calls[0].options.body.action, 'chest_status');
    assert.equal(panel.dataset.pending, '1');
    assert.equal(timers.length, 1);
    incoming = status('0');
    await timers.shift()();
    assert.equal(calls[1].url, '/runtime/estate/resources/status');
    assert.equal(calls[1].options.body, undefined); // Status checks never submit the game action again.
    assert.equal(panel.dataset.pending, '0');
    assert.equal(timers.length, 0);
    panel = status('1');
    context.mountEstateResourceStatus(card);
    incoming = status('0');
    incoming.dataset.profileId = '3';
    await timers.shift()();
    assert.equal(panel.dataset.profileId, '2');
    assert.match(feedback.textContent, /元神已切换/);
    assert.equal(timers.length, 0);
    console.log('Card submitter, named-action URL, completion polling and profile isolation: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
