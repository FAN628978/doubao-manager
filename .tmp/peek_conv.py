import asyncio, sys, json, sqlite3
from pathlib import Path
sys.path.insert(0,'.')
from playwright.async_api import async_playwright
URL = sys.argv[1]; ACC = sys.argv[2] if len(sys.argv)>2 else 'acc_9b661e5a38'
con=sqlite3.connect('data/manager.sqlite3'); con.row_factory=sqlite3.Row
profile=con.execute("SELECT profile_dir FROM accounts WHERE id=?",(ACC,)).fetchone()['profile_dir']
DUMP = r"""() => {
  const msgs = Array.from(document.querySelectorAll('[data-testid="send_message"],[data-testid="receive_message"]'));
  return {url: location.href, count: msgs.length,
    cards: document.querySelectorAll('[class*="block-video"]').length,
    failed: !!document.querySelector('[data-testid="conversation-list-v2-status-failed"]'),
    tail: msgs.slice(-8).map(m => ({role: m.getAttribute('data-testid'),
      text: (m.innerText||'').replace(/\s+/g,' ').slice(0,220)}))};
}"""
async def main():
    async with async_playwright() as p:
        c = await p.chromium.launch_persistent_context(user_data_dir=profile, channel='chrome',
              headless=False, no_viewport=True, locale='zh-CN', args=['--start-maximized'])
        pg = await c.new_page()
        await pg.goto(URL, wait_until='domcontentloaded', timeout=60000)
        await pg.wait_for_timeout(9000)
        for _ in range(4):
            await pg.mouse.wheel(0, 1500); await pg.wait_for_timeout(900)
        d = await pg.evaluate(DUMP)
        Path('.tmp/peek_conv.json').write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
        print('url=', d['url'], '| msgs=', d['count'], '| cards=', d['cards'], '| failed=', d['failed'])
        for m in d['tail']: print('  -', m['role'], ':', m['text'][:190])
        await c.close()
asyncio.run(main())
