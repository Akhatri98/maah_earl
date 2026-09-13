"""Headless application tests, isolated from the user's browser/profile.

Install requirements-dev.txt and Playwright Chromium. Start earl.web, then run
this script. Screenshots and measured layout checks are written to out/qa.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".tools" / "playwright"))


def finished(page):
    expect(page.locator("#run")).to_be_enabled(timeout=25000)
    expect(page.locator("#stage-notify")).to_have_class("passed")
    expect(page.locator("#artifact-frame")).to_be_visible()
    expect(page.locator("#artifact-empty")).to_be_hidden()


def run_case(page, scenario):
    page.locator("#scenario").select_option(scenario)
    page.locator("#run").click()
    finished(page)


def layout(page):
    return page.evaluate("""() => {
        const rect = element => { const r=element.getBoundingClientRect();
            return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}; };
        const panels = Object.fromEntries(['.controls','.scoreboard','.viewer','.results','.run-panel','.artifacts']
            .map(selector => [selector,rect(document.querySelector(selector))]));
        const overflow = [...document.querySelectorAll('button,select,textarea,h1,h2,.verdict,.reason,.capability')]
            .filter(e=> e.offsetWidth && !e.closest('.table-wrap') && e.getBoundingClientRect().right>innerWidth+1)
            .map(e=>({tag:e.tagName,id:e.id,text:e.textContent.slice(0,50)}));
        const canvas = document.createElement('canvas').getContext('2d');
        const compactSelects = [...document.querySelectorAll('select.compact')].map(element => {
            const style = getComputedStyle(element);
            canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
            return {id:element.id, longestLabel:Math.max(...[...element.options].map(o=>canvas.measureText(o.text).width)),
                available:element.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)-20};
        });
        return {viewport:{width:innerWidth,height:innerHeight}, documentWidth:document.documentElement.scrollWidth,
            documentHeight:document.documentElement.scrollHeight, panels, overflow, compactSelects,
            runButton:rect(document.querySelector('#run')),
            lastStage:rect(document.querySelector('#stage-notify')),
            memberCount:document.querySelectorAll('.member-line').length};
    }""")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--skip-ngrok-notice", action="store_true",
                        help="Use ngrok's documented test header; normal visitors still see its first-visit notice.")
    args = parser.parse_args()
    out = ROOT / "out" / "qa"
    out.mkdir(parents=True, exist_ok=True)
    report = {"url": args.url, "host_notice_test_header": args.skip_ngrok_notice, "layouts": {}, "checks": []}
    errors = []
    external_requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce",
                                      extra_http_headers={"ngrok-skip-browser-warning": "EARL application verification"}
                                      if args.skip_ngrok_notice else {})
        # Block third-party hosts, retaining only the local/public app origin.
        origin = args.url.rstrip("/")
        def route_request(route):
            if route.request.url.startswith(origin + "/"):
                route.continue_()
            else:
                external_requests.append(route.request.url)
                route.abort()
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        finished(page)
        expect(page.locator("#verdict")).to_have_text("APPROVED")
        expect(page.locator(".member-line")).to_have_count(10)
        expect(page.locator(".truss-joint")).to_have_count(6)
        expect(page.locator("#earl-grid button")).to_have_count(20)
        expect(page.locator("#baseline-grid button")).to_have_count(20)
        report["checks"].append("initial approval, six nodes, ten bars, 40 scoreboard cells")
        page.screenshot(path=str(out / "desktop-approved.png"), full_page=True)
        run_case(page, "thin-compression")
        expect(page.locator("#verdict")).to_have_text("ESCALATED")
        expect(page.locator("#governing")).to_contain_text("0.48")
        expect(page.locator('.member-group[data-mid="m8"] .member-line')).to_have_attribute("stroke", "#c13c48")
        thick = float(page.locator('.member-group[data-mid="m1"] .member-line').get_attribute("stroke-width"))
        thin = float(page.locator('.member-group[data-mid="m8"] .member-line').get_attribute("stroke-width"))
        assert thin < thick
        expect(page.frame_locator("#artifact-frame").locator("h1")).to_have_text("ESCALATED")
        expect(page.locator("#report-reference")).to_contain_text("NOT PERFORMED")
        page.locator('[data-artifact="email"]').click()
        expect(page.frame_locator("#artifact-frame").locator("pre")).to_contain_text("To: responsible.engineer@example.com")
        page.locator('[data-artifact="ecn"]').click()
        page.locator('[data-view="both"]').click()
        expect(page.locator(".member-line")).to_have_count(20)
        before_m8 = page.locator('.truss-frame').first.locator('.member-group[data-mid="m8"] .member-line')
        expect(before_m8).to_have_attribute("stroke", "#16825e")
        page.screenshot(path=str(out / "desktop-compare.png"), full_page=True)
        page.locator('[data-view="after"]').click()
        page.locator("#sort-sf").click()
        expect(page.locator("#member-rows tr").first.locator("td").first).to_have_text("m8")
        page.locator('.member-hit[data-member="m8"]').hover(position={"x":5,"y":5}, force=True)
        # Keyboard focus is deterministic even where diagonal hit regions cross.
        page.locator('.member-hit[data-member="m8"]').focus()
        expect(page.locator("#tooltip")).to_be_visible()
        expect(page.locator("#tooltip")).to_contain_text("Stress before")
        page.locator("#run").focus()
        report["checks"].append("red floor breach, area thickness, before/after, sort, hover, ECN and email")
        for name, size in (("desktop", (1440, 900)), ("laptop", (1366, 768)), ("tablet", (820, 1180)), ("mobile", (390, 844)), ("small-mobile", (320, 700))):
            page.set_viewport_size({"width": size[0], "height": size[1]})
            page.screenshot(path=str(out / f"{name}-escalated.png"), full_page=True)
            metrics = layout(page)
            assert metrics["documentWidth"] <= size[0], metrics
            assert not metrics["overflow"], metrics["overflow"]
            assert all(s["longestLabel"] <= s["available"] for s in metrics["compactSelects"]), metrics["compactSelects"]
            assert metrics["runButton"]["bottom"] <= metrics["panels"][".controls"]["bottom"], metrics
            assert metrics["lastStage"]["bottom"] <= metrics["panels"][".run-panel"]["bottom"], metrics
            report["layouts"][name] = metrics
        page.set_viewport_size({"width": 1366, "height": 768})
        run_case(page, "design-margin")
        expect(page.locator("#verdict")).to_have_text("ESCALATED")
        expect(page.locator("#verdict")).to_have_class("verdict escalated margin")
        run_case(page, "remove-m1")
        expect(page.locator(".member-line")).to_have_count(9)
        page.locator('[data-view="before"]').click()
        expect(page.locator(".member-line")).to_have_count(10)
        expect(page.locator('.member-group[data-mid="m1"] .member-line')).to_have_attribute("stroke", "#16825e")
        page.locator('[data-view="after"]').click()
        page.locator("#description").fill("resize m8 to 8 in2")
        expect(page.locator("#run")).to_be_disabled()
        page.locator("#parse").click()
        expect(page.locator("#parsed")).to_contain_text("member m8.area")
        expect(page.locator("#run")).to_be_enabled()
        page.locator("#run").click()
        finished(page)
        expect(page.locator("#verdict")).to_have_text("ESCALATED")
        report["checks"].append("amber design margin, member removal, typed edit shown before run")
        report["page_errors"] = errors
        report["third_party_requests"] = external_requests
        assert not errors, errors
        assert not external_requests, external_requests
        browser.close()
    report_name = "public-browser-report.json" if args.skip_ngrok_notice else "browser-report.json"
    (out / report_name).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
