"""The CBI website walk and the two internal endpoints.

The pages are trimmed captures of www.cbi.ir (2026-10-11): the section page's
year dropdown and month list (``cbi_section_2692.html``), the 1399 year list
with its three PDF-only months (``cbi_year_1399_20392.html``), and two month
pages whose «فرمت Excel» link comes first (Esfand 1404) and second (Tir 1399).
Only the markup the walk reads is kept; the structure and the links are as
served. The workbooks behind them are the real editions
tests/test_economic_cbi.py describes. No request leaves the machine: respx
answers every one, and an unexpected one fails the test.
"""
from __future__ import annotations

import gzip
import os
from datetime import datetime, timezone

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.db import data_providers, economic_observations, source_documents
from app.economic import cbi
from app.economic.cbi import (
    CbiBackfillRequiredError,
    CbiFetchError,
    CbiRunInProgressError,
    CbiSite,
    Edition,
    MonthLink,
    ingest_cbi_monetary,
    ingest_edition_file,
    newest_stored_edition,
    parse_excel_link,
    parse_month_list,
    parse_year_options,
)

from .conftest import FIXTURES_DIR, TEST_TOKEN, load_fixture_text

AUTH = {"X-Internal-Token": TEST_TOKEN}
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

SECTION = "https://www.cbi.ir/category/2692.aspx"
HTML = {"content-type": "text/html; charset=utf-8"}


def _xls(name: str) -> bytes:
    with gzip.open(os.path.join(FIXTURES_DIR, name)) as handle:
        return handle.read()


XLS_140412 = "cbi_1404-12_140412.xls.gz"
XLS_1403_07 = "cbi_1403-07_pol1403-7.xls.gz"


def _workbook_response(content: bytes, filename: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={
            "content-type": "application/vnd.ms-excel",
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )


def _site() -> CbiSite:
    return CbiSite(timeout=5.0, courtesy_delay=0.0, backoff_base=0.0)


def _month_page(excel_page_id: int) -> str:
    """A month page in the captured shape, pointing at ``/page/<id>.aspx``."""
    template = load_fixture_text("cbi_month_1404-12_35569.html")
    return template.replace("/page/35570.aspx", f"/page/{excel_page_id}.aspx")


def _year_page(year_months: dict[int, int]) -> str:
    """A year list in the captured 1399 shape, with ``{month: simplelist id}``."""
    from app.economic.jalali import JALALI_MONTHS

    items = "".join(
        f"<li ><a href='/simplelist/{page}.aspx'>\n\t\t\t{JALALI_MONTHS[month - 1]}\n\t\t\t</a></li>\n"
        for month, page in sorted(year_months.items())
    )
    return f'<html><body><ul class="simplelist">\n{items}</ul></body></html>'


@pytest.fixture()
def mocked():
    with respx.mock(assert_all_called=False) as router:
        yield router


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


# --- the pages ------------------------------------------------------------------


def test_the_year_dropdown_maps_every_year_to_its_list_page():
    years = parse_year_options(load_fixture_text("cbi_section_2692.html"))
    assert years[1404] == "https://www.cbi.ir/simplelist/33030.aspx"
    assert years[1399] == "https://www.cbi.ir/simplelist/20392.aspx"
    assert min(years) == 1385 and len(years) == 20


def test_the_section_page_lists_the_newest_years_twelve_month_pages():
    months = parse_month_list(load_fixture_text("cbi_section_2692.html"), 1404)
    assert [link.edition.label for link in months] == [f"1404-{m:02d}" for m in range(1, 13)]
    assert months[-1].url == "https://www.cbi.ir/simplelist/35569.aspx"
    assert all(link.excel for link in months)


def test_the_1399_list_marks_the_pdf_only_months():
    """Farvardin-Khordad 1399 link straight to a PDF; Tir on are month pages.
    The list spells the months with Arabic yeh («فروردين»)."""
    months = parse_month_list(load_fixture_text("cbi_year_1399_20392.html"), 1399)
    assert [link.edition.month for link in months] == list(range(1, 13))
    assert [link.excel for link in months] == [False] * 3 + [True] * 9
    assert months[0].url == "https://www.cbi.ir/page/20393.aspx"
    assert months[3].url == "https://www.cbi.ir/simplelist/20704.aspx"


def test_the_excel_link_is_found_by_its_label_wherever_it_is_listed():
    assert parse_excel_link(load_fixture_text("cbi_month_1404-12_35569.html")) == \
        "https://www.cbi.ir/page/35570.aspx"
    # Tir 1399 lists the PDF first and the workbook second.
    assert parse_excel_link(load_fixture_text("cbi_month_1399-04_20704.html")) == \
        "https://www.cbi.ir/page/20842.aspx"


def test_a_month_page_without_exactly_one_excel_link_is_refused():
    page = load_fixture_text("cbi_month_1404-12_35569.html")
    with pytest.raises(CbiFetchError, match="0 «فرمت Excel» links"):
        parse_excel_link(page.replace("فرمت Excel", "فرمت Word"))
    doubled = page.replace("</ul>", "<li><a href='/page/1.aspx'>فرمت  excel</a></li></ul>")
    with pytest.raises(CbiFetchError, match="2 «فرمت Excel» links"):
        parse_excel_link(doubled)


def test_only_same_site_list_and_page_links_are_ever_followed():
    page = (
        '<ul class="simplelist">'
        "<li><a href='https://evil.example/page/1.aspx'>فرمت Excel</a></li>"
        "<li><a href='javascript:alert(1)'>فرمت Excel</a></li>"
        "</ul>"
    )
    with pytest.raises(CbiFetchError, match="0 «فرمت Excel» links"):
        parse_excel_link(page)
    # Not followed -- and not skipped in silence either: a month that cannot be
    # followed is a month the run would never ingest.
    with pytest.raises(CbiFetchError, match="links مهر to '//cdn.example/simplelist/1.aspx'"):
        parse_month_list(
            "<ul class='simplelist'><li><a href='//cdn.example/simplelist/1.aspx'>مهر</a></li></ul>",
            1404,
        )


def test_a_month_list_item_that_is_not_a_month_name_refuses_the_page():
    """Every year list CBI has published is the twelve month names and nothing
    else. Were the labels to change («فروردین ۱۴۰۵»), skipping what does not
    parse would leave the scheduled run reporting success with no editions
    found, forever; a backfill would pass over the edition and then refuse it
    as older than stored."""
    page = load_fixture_text("cbi_section_2692.html").replace(
        "\t\t\tاسفند\n", "\t\t\tاسفند ۱۴۰۴\n"
    )
    assert "اسفند ۱۴۰۴" in page
    with pytest.raises(CbiFetchError, match="«اسفند ۱۴۰۴» that is not a Jalali month name"):
        parse_month_list(page, 1404)
    # Even for the newest year, whose list may legitimately be empty.
    with pytest.raises(CbiFetchError, match="not a Jalali month name"):
        parse_month_list(page, 1404, allow_empty=True)


def test_relabelled_months_fail_the_scheduled_run_instead_of_finding_nothing(
    client, engine, tmp_path, mocked, quiet_site
):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    relabelled = load_fixture_text("cbi_section_2692.html")
    from app.economic.jalali import JALALI_MONTHS

    for name in JALALI_MONTHS:
        relabelled = relabelled.replace(f"\t\t\t{name}\n", f"\t\t\t{name} 1404\n")
    mocked.get(SECTION).mock(return_value=httpx.Response(200, text=relabelled, headers=HTML))
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_failed"
    assert "not a Jalali month name" in resp.json()["error"]["message"]


def test_a_page_without_the_markup_is_a_fetch_failure_not_an_empty_answer():
    with pytest.raises(CbiFetchError, match="no year list"):
        parse_year_options("<html><body>Service unavailable</body></html>")
    with pytest.raises(CbiFetchError, match="month list"):
        parse_month_list("<html></html>", 1404)


# --- the download ---------------------------------------------------------------


def test_a_workbook_is_accepted_by_its_ole_signature(mocked):
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))
    site = _site()
    download = site.download(MonthLink(Edition(1404, 12), "https://www.cbi.ir/simplelist/35569.aspx", True))
    assert download.filename == "140412.xls"
    assert download.content[:4] == bytes.fromhex("d0cf11e0")
    assert download.url == "https://www.cbi.ir/page/35570.aspx"
    assert site.requests == 2


def test_an_html_page_where_the_workbook_should_be_fails_the_run(mocked):
    """A challenge or an error page: reported, never retried with other
    headers, never worked around."""
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    challenge = mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=httpx.Response(200, text="<html><script>challenge()</script></html>", headers=HTML))
    with pytest.raises(CbiFetchError, match="answered an HTML page where the Excel workbook should be"):
        _site().download(MonthLink(Edition(1404, 12), "https://www.cbi.ir/simplelist/35569.aspx", True))
    assert challenge.call_count == 1


def test_an_access_denial_is_not_retried(mocked):
    denied = mocked.get(SECTION).mock(return_value=httpx.Response(403))
    with pytest.raises(CbiFetchError, match="access denied"):
        _site().page(SECTION)
    assert denied.call_count == 1


def test_a_failing_resolver_is_retried_with_backoff(mocked):
    """The production host's resolver times out before it answers; a lookup
    failure is transient and costs an attempt, not the run."""
    route = mocked.get(SECTION).mock(side_effect=[
        httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx.ConnectTimeout("timed out"),
        httpx.Response(200, text=load_fixture_text("cbi_section_2692.html"), headers=HTML),
    ])
    assert "ddlYearList" in _site().page(SECTION)
    assert route.call_count == 3


def test_the_budget_is_sized_for_the_production_hosts_resolver():
    site = cbi.make_site()
    assert site.timeout >= 60
    assert site.max_attempts >= 3
    assert site.backoff_base > 0 and site.courtesy_delay >= 1.0
    assert site.user_agent == "IranGoldPredictor/1.0 (+self-hosted analytics)"


def test_giving_up_after_the_last_attempt_is_a_fetch_failure(mocked):
    route = mocked.get(SECTION).mock(return_value=httpx.Response(503))
    with pytest.raises(CbiFetchError, match="failed after 4 attempts"):
        _site().page(SECTION)
    assert route.call_count == 4


# --- runs -----------------------------------------------------------------------


def _serve_section(mocked):
    return mocked.get(SECTION).mock(return_value=httpx.Response(
        200, text=load_fixture_text("cbi_section_2692.html"), headers=HTML))


def test_fetch_latest_needs_the_backfill_first(engine, mocked):
    with pytest.raises(CbiBackfillRequiredError, match='backfill_from": "1399-04"'):
        ingest_cbi_monetary(engine, fetch_latest=True, site=_site())
    assert not mocked.calls


def test_fetch_latest_rechecks_the_newest_stored_edition(engine, mocked, tmp_path):
    """Three requests: the section page (which carries 1404's months), the
    Esfand month page, the workbook. Same bytes: nothing written."""
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))

    report = ingest_cbi_monetary(engine, fetch_latest=True, site=_site())
    assert report["status"] == "ok" and report["failed"] is None
    assert report["requests"] == 3
    assert [e["edition"] for e in report["editions"]] == ["1404-12"]
    assert report["editions"][0]["unchanged"] == 12
    assert report["editions"][0]["available_at_approx"] is False
    assert report["newest_stored_before"] == report["newest_stored_after"] == "1404-12"
    assert report["inserted"] == report["revised"] == 0


def test_a_backfill_walks_the_years_oldest_first_and_stops_at_the_first_failure(engine, mocked):
    """1403-07 is stored; the next listed edition (1404-01) comes back as a
    workbook whose own title says 1404-12, which is refused — so the run stops
    there, reports it, and keeps what it stored before it. The next run would
    retry 1404-01 first; carrying on would lock it out as 'older'."""
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/30380.aspx").mock(
        return_value=httpx.Response(200, text=_year_page({6: 31000, 7: 31402}), headers=HTML))
    mocked.get("https://www.cbi.ir/simplelist/31402.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(31403), headers=HTML))
    mocked.get("https://www.cbi.ir/page/31403.aspx").mock(
        return_value=_workbook_response(_xls(XLS_1403_07), "pol1403,7.xls"))
    mocked.get("https://www.cbi.ir/simplelist/33031.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(33058), headers=HTML))
    mocked.get("https://www.cbi.ir/page/33058.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))
    later = mocked.get("https://www.cbi.ir/simplelist/33032.aspx")

    report = ingest_cbi_monetary(engine, backfill_from="1403-07", site=_site(), now=NOW)

    assert report["mode"] == "backfill" and report["status"] == "failed"
    assert [e["edition"] for e in report["editions"]] == ["1403-07", "1404-01"]
    stored, failed = report["editions"]
    assert stored["status"] == "ok" and stored["inserted"] == 12
    assert stored["available_at"] == "2024-12-08T09:58:22+00:00" and stored["available_at_approx"]
    assert failed["status"] == "error"
    assert report["failed"]["edition"] == "1404-01"
    assert report["failed"]["kind"] == "CbiParseError"
    assert "the workbook's own title says 1404-12" in report["failed"]["error"]
    assert not later.called
    assert newest_stored_edition(engine) == Edition(1403, 7)
    # Section, 1403's year page, then a month page and a workbook for each.
    assert report["requests"] == 6


def test_a_new_year_in_the_dropdown_before_its_first_month_is_not_a_failure(engine, mocked, tmp_path):
    """At Nowruz the section page can name 1405 before any 1405 month exists.
    The walk reads 1404 from its year page and reports 1405 as empty."""
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    section = load_fixture_text("cbi_section_2692.html")
    head, _, _ = section.partition('<ul class="simplelist">')
    section_1405 = head.replace('<option value="33030">1404</option>',
                                '<option value="36000">1405</option>\n\t<option value="33030">1404</option>')
    mocked.get(SECTION).mock(return_value=httpx.Response(200, text=section_1405, headers=HTML))
    mocked.get("https://www.cbi.ir/simplelist/33030.aspx").mock(
        return_value=httpx.Response(200, text=section, headers=HTML))
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))

    report = ingest_cbi_monetary(engine, fetch_latest=True, site=_site())
    assert report["status"] == "ok"
    assert report["years_without_months"] == [1405]
    assert [e["edition"] for e in report["editions"]] == ["1404-12"]
    # A year page that has lost its list is still a failure.
    with pytest.raises(CbiFetchError, match="1404 month list"):
        parse_month_list(head, 1404)


def test_a_backfill_resumes_after_the_newest_stored_edition(engine, mocked, tmp_path):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))
    report = ingest_cbi_monetary(engine, backfill_from="1399-04", site=_site(), now=NOW)
    assert report["skipped_older_than_stored"]["before"] == "1404-12"
    # No year page before 1404 was read, and only the stored edition re-checked.
    assert report["requests"] == 3
    assert [e["edition"] for e in report["editions"]] == ["1404-12"]


def test_a_backfill_from_before_the_excel_era_starts_at_tir_1399(engine, mocked):
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/20392.aspx").mock(
        return_value=httpx.Response(200, text=load_fixture_text("cbi_year_1399_20392.html"), headers=HTML))
    # The first listed Excel edition is refused at download: enough to see where it started.
    mocked.get("https://www.cbi.ir/simplelist/20704.aspx").mock(
        return_value=httpx.Response(200, text=load_fixture_text("cbi_month_1399-04_20704.html"), headers=HTML))
    mocked.get("https://www.cbi.ir/page/20842.aspx").mock(return_value=httpx.Response(503))
    for year_id in (21999, 23808, 27853, 30380):
        mocked.get(f"https://www.cbi.ir/simplelist/{year_id}.aspx").mock(
            return_value=httpx.Response(200, text=_year_page({}), headers=HTML))
    report = ingest_cbi_monetary(engine, backfill_from="1398-01", site=_site(), now=NOW)
    assert "before 1399-04" in report["note"]
    assert report["editions"][0]["edition"] == "1399-04"
    assert report["failed"]["kind"] == "CbiFetchError"
    # Farvardin-Khordad 1399 are PDFs and were never requested.
    assert report["pdf_only"] == []


def _serve_backfill_1400_12_to_1403_07(router):
    """A website holding three editions in order: Esfand 1400 (base money on
    the old method), Esfand 1402 (which restates Esfand 1400 on the revised
    one) and Mehr 1403 (saved 2024-12-07, BEFORE the Esfand 1402 re-save, so a
    backfill clamps its availability). 1404's list is empty."""
    section = load_fixture_text("cbi_section_2692.html")
    head, _, _ = section.partition('<ul class="simplelist">')
    router.get(SECTION).mock(return_value=httpx.Response(200, text=head, headers=HTML))
    for year_id, months in ((21999, {12: 50012}), (23808, {}), (27853, {12: 50212}),
                            (30380, {7: 50307})):
        router.get(f"https://www.cbi.ir/simplelist/{year_id}.aspx").mock(
            return_value=httpx.Response(200, text=_year_page(months), headers=HTML))
    for month_id, fixture, name in (
        (50012, "cbi_1400-12_pol1400-12.xls.gz", "pol1400,12.xls"),
        (50212, "cbi_1402-12_pol1402-12.xls.gz", "pol1402,12.xls"),
        (50307, XLS_1403_07, "pol1403,7.xls"),
    ):
        router.get(f"https://www.cbi.ir/simplelist/{month_id}.aspx").mock(
            return_value=httpx.Response(200, text=_month_page(month_id + 1), headers=HTML))
        router.get(f"https://www.cbi.ir/page/{month_id + 1}.aspx").mock(
            return_value=_workbook_response(_xls(fixture), name))


def test_a_dry_run_backfill_reports_what_the_real_backfill_then_stores(engine):
    """Each edition in a dry run is measured against the editions before it in
    the same run, as the real run measures it. Rolled back edition by edition,
    the dry run showed the Esfand 1402 workbook as twelve fresh inserts -- not
    the base-money restatement of Esfand 1400 it carries -- and Mehr 1403 as
    available from 2024-12-08, where the real backfill stores 2024-12-31."""
    def editions(report):
        return [
            (e["edition"], e["inserted"], e["unchanged"], e["revised"],
             e["available_at"], e["available_at_approx"],
             e["series"]["CBI_BASE_MONEY"]["revisions"])
            for e in report["editions"]
        ]

    with respx.mock(assert_all_called=False) as router:
        _serve_backfill_1400_12_to_1403_07(router)
        dry = ingest_cbi_monetary(engine, backfill_from="1400-12", dry_run=True,
                                  site=_site(), now=NOW)
    assert dry["status"] == "ok" and dry["dry_run"] is True
    assert _count(engine, economic_observations) == 0
    assert _count(engine, source_documents) == 0
    assert newest_stored_edition(engine) is None

    with respx.mock(assert_all_called=False) as router:
        _serve_backfill_1400_12_to_1403_07(router)
        real = ingest_cbi_monetary(engine, backfill_from="1400-12", site=_site(), now=NOW)
    assert real["status"] == "ok"
    assert editions(dry) == editions(real)
    assert (dry["inserted"], dry["unchanged"], dry["revised"]) == \
        (real["inserted"], real["unchanged"], real["revised"])
    # What the comparison is about, spelled out.
    _, restated, mehr = editions(dry)
    assert restated[3] == 1 and restated[6][0]["period"] == "1400-12"
    assert restated[6][0]["from_method"] == "old" and restated[6][0]["to_method"] == "revised"
    assert mehr[4] == "2024-12-31T07:17:53+00:00"


def test_only_one_cbi_run_at_a_time(client, engine, tmp_path, mocked, quiet_site):
    """The backfill takes about half an hour and is started by hand. A run that
    lands inside it -- the daily tick, a posted workbook -- would read the
    backfill's progress as the newest stored edition, stamp every later
    edition with today's availability, and lock the backfill out of them. It
    is refused instead, and nothing is touched."""
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    assert cbi._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(CbiRunInProgressError, match="another CBI ingest is running"):
            ingest_cbi_monetary(engine, fetch_latest=True, site=_site())
        with pytest.raises(CbiRunInProgressError):
            ingest_cbi_monetary(engine, path=str(path))
        resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True},
                           headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "run_in_progress"
    finally:
        cbi._RUN_LOCK.release()
    assert not mocked.calls
    assert _count(engine, source_documents) == 1
    # Released after every run, including a failed one.
    with pytest.raises(ValueError, match="backfill_from"):
        ingest_cbi_monetary(engine, backfill_from="Tir 1399", site=_site())
    assert cbi._RUN_LOCK.acquire(blocking=False)
    cbi._RUN_LOCK.release()


def test_an_operator_switch_stops_every_request(engine, mocked):
    with engine.begin() as conn:
        conn.execute(data_providers.insert().values(
            code="cbi", name="Central Bank of Iran", base_url="https://www.cbi.ir",
            category="iran_macro", priority=40, enabled=False,
        ))
    report = ingest_cbi_monetary(engine, fetch_latest=True, site=_site())
    assert report["status"] == "skipped" and "enabled is FALSE" in report["reason"]
    assert not mocked.calls


def test_exactly_one_mode_is_required(engine):
    for kwargs in ({}, {"fetch_latest": True, "path": "/tmp/x.xls"},
                   {"backfill_from": "1399-04", "fetch_latest": True}):
        with pytest.raises(ValueError, match="exactly one of"):
            ingest_cbi_monetary(engine, **kwargs)
    with pytest.raises(ValueError, match="backfill_from"):
        ingest_cbi_monetary(engine, backfill_from="Tir 1399", site=_site())


# --- the endpoints --------------------------------------------------------------


@pytest.fixture()
def quiet_site(monkeypatch):
    monkeypatch.setattr(cbi, "make_site", _site)


@pytest.fixture()
def provider_row(engine):
    with engine.begin() as conn:
        conn.execute(data_providers.insert().values(
            code="cbi", name="Central Bank of Iran", base_url="https://www.cbi.ir",
            category="iran_macro", priority=40, enabled=True,
        ))

    def health():
        with engine.connect() as conn:
            row = conn.execute(select(data_providers).where(data_providers.c.code == "cbi")).one()
            return row.consecutive_failures, row.last_error
    health.last_success = lambda: _last_success(engine)
    return health


def _last_success(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(data_providers.c.last_success_at).where(data_providers.c.code == "cbi")
        ).scalar()


def test_the_endpoints_require_the_internal_token(client):
    assert client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}).status_code == 401
    assert client.post("/internal/economic/cbi-history", json={"path": "x"}).status_code == 401


def test_a_posted_workbook_is_ingested_and_reported(client, engine, tmp_path, provider_row):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    resp = client.post("/internal/economic/cbi-monetary",
                       json={"path": str(path), "filename": "140412.xls"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "path" and body["edition"] == "1404-12"
    assert body["inserted"] == 12
    assert body["series"]["CBI_LIQUIDITY"]["values"][-1] == "155812.2"
    assert provider_row() == (0, None)
    # A posted file says nothing about whether cbi.ir answers: the provider
    # is not marked healthy by it.
    assert provider_row.last_success() is None
    again = client.post("/internal/economic/cbi-monetary", json={"path": str(path)}, headers=AUTH)
    assert again.status_code == 200 and again.json()["unchanged"] == 12
    assert provider_row.last_success() is None


def test_a_posted_workbook_does_not_clear_a_failing_website(client, engine, tmp_path, mocked,
                                                           quiet_site, provider_row):
    """The daily job failing against cbi.ir must stay visible on the provider
    row when an operator ingests a workbook by hand meanwhile."""
    with engine.begin() as conn:
        conn.execute(data_providers.update().where(data_providers.c.code == "cbi").values(
            consecutive_failures=3, last_error="www.cbi.ir: timed out"))
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    resp = client.post("/internal/economic/cbi-monetary", json={"path": str(path)}, headers=AUTH)
    assert resp.status_code == 200
    assert provider_row() == (3, "www.cbi.ir: timed out")
    assert provider_row.last_success() is None


def test_a_refused_workbook_answers_400_and_marks_the_provider(client, engine, tmp_path, provider_row):
    path = tmp_path / "140412.xls"
    path.write_bytes(b"<html>blocked</html>")
    resp = client.post("/internal/economic/cbi-monetary", json={"path": str(path)}, headers=AUTH)
    assert resp.status_code == 400
    assert "HTML page" in resp.json()["error"]["message"]
    failures, error = provider_row()
    assert failures == 1 and "HTML page" in error
    assert _count(engine, economic_observations) == 0


def test_an_older_edition_answers_409(client, tmp_path, provider_row):
    newer, older = tmp_path / "140412.xls", tmp_path / "pol1403,7.xls"
    newer.write_bytes(_xls(XLS_140412))
    older.write_bytes(_xls(XLS_1403_07))
    assert client.post("/internal/economic/cbi-monetary", json={"path": str(newer)}, headers=AUTH).status_code == 200
    resp = client.post("/internal/economic/cbi-monetary", json={"path": str(older)}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "stale_edition"
    # CBI published nothing wrong.
    assert provider_row() == (0, None)
    replay = client.post("/internal/economic/cbi-monetary",
                         json={"path": str(older), "allow_older": True}, headers=AUTH)
    assert replay.status_code == 200


def test_the_scheduled_call_before_the_backfill_answers_409(client, mocked, quiet_site):
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "backfill_required"
    assert not mocked.calls


def test_malformed_calls_answer_400(client):
    for body in ({}, {"fetch_latest": True, "backfill_from": "1399-04"},
                 {"backfill_from": "1399/04"}, {"path": "/tmp/there-is-no-such-workbook.xls"}):
        resp = client.post("/internal/economic/cbi-monetary", json=body, headers=AUTH)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == "bad_request"


def test_a_failed_website_run_answers_502_with_its_report(client, engine, tmp_path, mocked,
                                                          quiet_site, provider_row):
    """The Go scheduler records success from the status code; a run that could
    not get the workbook must not enter the job history as a success."""
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=httpx.Response(200, text="<html>Request Rejected</html>", headers=HTML))

    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "upstream_failed"
    assert body["failed"]["edition"] == "1404-12"
    assert body["editions"][0]["status"] == "error"
    failures, error = provider_row()
    assert failures == 1 and "1404-12" in error


def test_a_website_run_that_reads_cbi_marks_the_provider_healthy(client, engine, tmp_path, mocked,
                                                                 quiet_site, provider_row):
    """The other side of path mode recording nothing: a run that did read
    cbi.ir -- here finding the edition already stored -- clears the failures."""
    with engine.begin() as conn:
        conn.execute(data_providers.update().where(data_providers.c.code == "cbi").values(
            consecutive_failures=2, last_error="www.cbi.ir: timed out"))
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_140412), "140412.xls"))
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 200, resp.json()
    assert provider_row() == (0, None)
    assert provider_row.last_success() is not None


def test_a_refused_fetched_workbook_answers_502_document_refused(client, engine, tmp_path, mocked,
                                                                quiet_site):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    _serve_section(mocked)
    mocked.get("https://www.cbi.ir/simplelist/35569.aspx").mock(
        return_value=httpx.Response(200, text=_month_page(35570), headers=HTML))
    # The Mehr 1403 workbook where Esfand 1404's should be.
    mocked.get("https://www.cbi.ir/page/35570.aspx").mock(
        return_value=_workbook_response(_xls(XLS_1403_07), "140412.xls"))
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "document_refused"


def test_an_unreadable_section_page_answers_502(client, engine, tmp_path, mocked, quiet_site):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    ingest_edition_file(engine, str(path), now=NOW)
    mocked.get(SECTION).mock(return_value=httpx.Response(200, text="<html>maintenance</html>", headers=HTML))
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_failed"


def test_a_dry_run_writes_nothing_and_leaves_health_alone(client, engine, tmp_path, provider_row):
    path = tmp_path / "140412.xls"
    path.write_bytes(_xls(XLS_140412))
    resp = client.post("/internal/economic/cbi-monetary",
                       json={"path": str(path), "dry_run": True}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["inserted"] == 12
    assert _count(engine, economic_observations) == 0
    assert _count(engine, source_documents) == 0


def test_the_switched_off_provider_is_skipped_not_failed(client, engine, mocked, quiet_site):
    with engine.begin() as conn:
        conn.execute(data_providers.insert().values(
            code="cbi", name="Central Bank of Iran", base_url="https://www.cbi.ir",
            category="iran_macro", priority=40, enabled=False,
        ))
    resp = client.post("/internal/economic/cbi-monetary", json={"fetch_latest": True}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["status"] == "skipped"
    assert not mocked.calls
