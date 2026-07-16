const fs = require('fs');
const vm = require('vm');

// Mock HTML structure and DOM elements
const mockElements = {
    'tab-entities': { classList: { add: (c) => console.log('tab-entities active'), remove: (c) => {} } },
    'tab-events': { classList: { add: (c) => console.log('tab-events active'), remove: (c) => {} } },
    'tab-tags': { classList: { add: (c) => console.log('tab-tags active'), remove: (c) => {} } },
    'tab-relations': { classList: { add: (c) => console.log('tab-relations active'), remove: (c) => {} } },
    'tab-locks': { classList: { add: (c) => console.log('tab-locks active'), remove: (c) => {} } },
    'entities-list': { innerHTML: '', appendChild: (c) => {} },
    'events-list': { innerHTML: '', appendChild: (c) => {} },
    'tags-list': { innerHTML: '', appendChild: (c) => {} },
    'locks-list': { innerHTML: '', appendChild: (c) => {} },
    'relations-sidebar-list': { innerHTML: '', appendChild: (c) => {} },
    'relations-svg': { clientWidth: 800, clientHeight: 600, innerHTML: '', appendChild: (c) => {}, clientRect: { left: 0, top: 0 }, getBoundingClientRect: () => ({ left: 0, top: 0 }) }
};

const mockButtons = [
    { classList: { add: (c) => {}, remove: (c) => {} } },
    { classList: { add: (c) => {}, remove: (c) => {} } },
    { classList: { add: (c) => {}, remove: (c) => {} } },
    { classList: { add: (c) => {}, remove: (c) => {} } },
    { classList: { add: (c) => {}, remove: (c) => {} } }
];

const mockDocument = {
    querySelectorAll: (selector) => {
        if (selector === '.tab-btn') return mockButtons;
        if (selector === '.view-content') return Object.values(mockElements).slice(0, 5);
        return [];
    },
    getElementById: (id) => {
        return mockElements[id] || null;
    },
    createElement: (tag) => {
        return { style: {}, classList: { add: () => {} } };
    },
    createElementNS: (ns, tag) => {
        return { setAttribute: () => {}, appendChild: () => {}, style: {} };
    }
};

const mockFetch = (url) => {
    return Promise.resolve({
        json: () => {
            if (url.includes('/api/entities')) {
                return Promise.resolve({ entities: [], pagination: { page: 1, limit: 100, total: 0, pages: 1 } });
            }
            if (url.includes('/api/relations')) {
                return Promise.resolve([]);
            }
            return Promise.resolve({});
        }
    });
};

const context = {
    document: mockDocument,
    window: {},
    console: console,
    fetch: mockFetch,
    setTimeout: (fn) => fn(),
    Date: Date,
    Math: Math
};

vm.createContext(context);

const jsCode = fs.readFileSync('scratch/viewer_debug.js', 'utf8');

try {
    vm.runInContext(jsCode, context);
    console.log("Compilation and execution check completed successfully.");
    
    // Test switchTab
    console.log("Testing switchTab('entities')...");
    context.switchTab('entities');
    
    console.log("Testing switchTab('relations')...");
    context.switchTab('relations');
    
} catch (e) {
    console.error("Execution error detected:", e);
}
