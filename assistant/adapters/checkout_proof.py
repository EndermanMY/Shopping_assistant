"""Browser-local checkout evidence based on order values, not DOM decoration."""
import time

CAPTURE = r"""s => {
    const norm = value => String(value ?? '').replace(/\s+/g, ' ').trim();
    const nodes = selector => selector ? [...document.querySelectorAll(selector)] : [];
    const visible = n => !!n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
    const text = n => norm(n.innerText ?? n.textContent);
    const snapshot = () => JSON.stringify({
        url: location.href,
        goods: nodes(s.goods).map(n => [visible(n),
            [n.getAttribute('data-sku'), n.getAttribute('skuid')],
            [...n.querySelectorAll('a[href*="item.jd.com/"]')].map(a => [a.href.split(/[?#]/)[0], text(a)]),
            [...n.querySelectorAll('[class^="title-"] [class^="ellipsis-"]')].map(text),
            [...new Set(n.querySelectorAll(s.quantity+', [class^="quantity-"]'))].map(text)]),
        address: nodes(s.address).map(n => [visible(n), text(n),
            [...n.querySelectorAll('input,select,textarea')].map(i => [i.value, i.checked])]),
        total: nodes(s.total).map(n => [visible(n), text(n)]),
        payment: nodes(s.payment).map(n => [visible(n), text(n),
            [...n.querySelectorAll('input')].map(i => [i.type, i.value, i.checked])])
    });
    // Only SKU navigation callbacks affect cache reuse. Other React callbacks
    // and decoration never determine the order's business identity.
    const handlers = () => nodes('[class^="title-"] [class^="ellipsis-"]').flatMap(n =>
        [n.parentElement, n].flatMap(el => {
            const values = [el.onclick];
            for (const k of Object.getOwnPropertyNames(el).filter(k => k.startsWith('__reactProps$')))
                values.push(el[k]?.onClick);
            return values;
        }));
    return {snapshot, values: snapshot(), handlers, handlerValues: handlers()};
}"""

MATCHES = r"""(p, checkHandlers) => {
    if (p.snapshot() !== p.values) return false;
    if (!checkHandlers) return true;
    const handlers = p.handlers();
    return handlers.length === p.handlerValues.length && handlers.every((f,i) => f === p.handlerValues[i]);
}"""


class CheckoutProof:
    def __init__(self, handle, key):
        self.handle, self.key = handle, key
        self.created = time.monotonic()

    @staticmethod
    def config_key(config):
        return (config.product.sku, config.product.spec, config.quantity,
                config.max_total_cents, config.address_hint, config.ignore_price, config.skip_checks)

    @classmethod
    async def capture(cls, page, selectors, config):
        return cls(await page.evaluate_handle(CAPTURE, selectors), cls.config_key(config))

    async def matches(self, config, *, check_handlers=True):
        if self.key != self.config_key(config) or time.monotonic()-self.created > 60:
            return False
        try:
            return await self.handle.evaluate(MATCHES, check_handlers)
        except Exception:
            return False

    async def close(self):
        try:
            await self.handle.dispose()
        except Exception:
            pass

    async def changes(self):
        try:
            return await self.handle.evaluate("""p => {
                const before=JSON.parse(p.values), after=JSON.parse(p.snapshot());
                return Object.keys(before).filter(k => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
            }""")
        except Exception:
            return ['url']
