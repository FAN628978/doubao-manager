import asyncio, sys
sys.path.insert(0,'.')
from playwright.async_api import async_playwright
from web_api import read_state, VIDEO_CARD
import tests.test_doubao_flow as T

HTML = T.HTML.replace('__MODE__','card_no_player')
async def main():
    async with async_playwright() as pw:
        b = await T.launch_chromium(pw)
        pg = await b.new_page()
        await pg.set_content(HTML)
        await pg.fill('textarea#prompt','测试提示词')
        await pg.click('[data-testid="chat_input"] button')
        await pg.wait_for_timeout(300)
        await pg.click('[data-testid="chat_input"] button')
        await pg.wait_for_timeout(900)
        s = await read_state(pg)
        print("cards=", s["videoCards"], "runningLast=", s["runningLast"], "awaitingConfirm=", s["awaitingConfirm"],
              "failed=", s["failed"], "lastText=", repr(s["lastMessageText"][:80]))
        print("card locator count=", await pg.locator(VIDEO_CARD).count())
        print("sends=", await pg.evaluate('window.__sends'))
        await b.close()
asyncio.run(main())
