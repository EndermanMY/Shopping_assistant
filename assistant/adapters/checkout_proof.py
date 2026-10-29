"""Reuse verified checkout identity only while its critical DOM stays unchanged."""
import time


CAPTURE = r"""selectors => {
    const collect = () => [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))];
    const signature = node => JSON.stringify([
        node.outerHTML, [node, ...node.querySelectorAll('input,select,textarea')].map(n =>
            [n.value ?? null, n.checked ?? null, n.disabled ?? null]),
        !!node.getClientRects().length, getComputedStyle(node).visibility
    ]);
    const nodes = collect();
    const proof = {url: location.href, nodes, collect, signature, values: nodes.map(signature), valid: true};
    proof.handlers = () => nodes.flatMap(root => [root, ...root.querySelectorAll('*')]).flatMap(n => {
        const fns = [n.onclick, n.onchange, n.onkeydown];
        for (const key of Object.getOwnPropertyNames(n).filter(k => k.startsWith('__reactProps$'))) {
            const props = n[key];
            for (const name of Object.keys(props || {}).filter(k => /^on[A-Z]/.test(k)).sort()) fns.push(props[name]);
        }
        return fns;
    });
    proof.handlerValues = proof.handlers();
    proof.observer = new MutationObserver(records => {
        for (const record of records) {
            if (nodes.some(n => n === record.target || n.contains(record.target) ||
                [...record.removedNodes].some(r => r === n || r.contains(n)))) {
                proof.valid = false;
                break;
            }
        }
    });
    proof.observer.observe(document.documentElement, {subtree:true, childList:true, attributes:true, characterData:true});
    return proof;
}"""

MATCHES = r"""p => {
    if (!p.valid || p.url !== location.href) return false;
    const nodes = p.collect();
    const handlers = p.handlers();
    return handlers.length === p.handlerValues.length && handlers.every((f,i) => f === p.handlerValues[i]) &&
        nodes.length === p.nodes.length && nodes.every((n,i) =>
        n === p.nodes[i] && p.signature(n) === p.values[i]);
}"""


class CheckoutProof:
    def __init__(self, handle, key):
        self.handle, self.key = handle, key
        self.created = time.monotonic()

    @staticmethod
    def config_key(config):
        return (config.product.sku, config.product.spec, config.quantity,
                config.max_total_cents, config.address_hint, getattr(config, 'submit_mode', 'dom'))

    @classmethod
    async def capture(cls, page, selectors, config):
        return cls(await page.evaluate_handle(CAPTURE, selectors), cls.config_key(config))

    async def matches(self, config):
        if self.key != self.config_key(config) or time.monotonic()-self.created > 60:
            return False
        try:
            return await self.handle.evaluate(MATCHES)
        except Exception:
            return False

    async def close(self):
        try:
            await self.handle.evaluate('p => p.observer.disconnect()')
            await self.handle.dispose()
        except Exception:
            pass
