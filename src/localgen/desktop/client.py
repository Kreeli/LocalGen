import json
import logging

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest

logger = logging.getLogger(__name__)


class Client(QObject):
    def __init__(self, port, token, parent):
        super().__init__(parent)
        self.base = f"http://127.0.0.1:{port}"
        self.token = QByteArray(f"Bearer {token}".encode())
        self.manager = QNetworkAccessManager(self)
        self.manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))

    def request(self, path, callback, *, payload=None, post=False, png=False, key=None, delete=False):
        if not path.startswith("/api/v1/"):
            callback(None, "接口路径无效")
            return
        request = QNetworkRequest(QUrl(self.base + path))
        request.setRawHeader(QByteArray(b"Authorization"), self.token)
        if key:
            request.setRawHeader(QByteArray(b"Idempotency-Key"), QByteArray(key.encode("ascii")))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        request.setTransferTimeout(8000)
        if post:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self.manager.post(request, QByteArray(json.dumps(payload or {}, ensure_ascii=False).encode()))
        elif delete:
            reply = self.manager.deleteResource(request)
        else:
            reply = self.manager.get(request)

        def finished():
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            content = bytes(reply.readAll())
            if code == 0:
                logger.debug("DESKTOP_API_CONNECTION_FAILED %s", reply.errorString())
            reply.deleteLater()
            if png and 200 <= code < 300:
                callback(content, None)
                return
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError):
                result = {}
            if 200 <= code < 300:
                callback(result, None)
            else:
                callback(None, result.get("error", {}).get("message", "本机服务暂不可用"))

        reply.finished.connect(finished)
