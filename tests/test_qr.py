import requests

from feishu_adapter.qr import QRViewer


def test_qr_lifecycle_and_host_guard():
    viewer = QRViewer()
    session = requests.Session()
    session.trust_env = False
    try:
        viewer.show(b'<svg id="TEST_ONLY"/>')
        response = session.get(viewer.url)
        assert response.status_code == 200 and "TEST_ONLY" in response.text
        assert response.headers["Cache-Control"].startswith("no-store")
        assert (
            session.get(viewer.url, headers={"Host": "evil.example"}).status_code == 403
        )
        assert (
            session.get(
                viewer.url, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            session.get(
                viewer.url, headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code
            == 403
        )
        assert session.get(viewer.url + "refresh").status_code == 403
        assert session.post(viewer.url).status_code == 501
        viewer.status("授权成功", done=True)
        response = session.get(viewer.url)
        assert (
            "TEST_ONLY" not in response.text
            and 'http-equiv="refresh"' not in response.text
        )
    finally:
        viewer.close()
        session.close()
