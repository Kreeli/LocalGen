import json
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import (
    QHttpMultiPart, QHttpPart, QNetworkAccessManager, QNetworkProxy, QNetworkRequest,
)

logger = logging.getLogger(__name__)
# 参考图允许的扩展名 → multipart 里声明的 MIME（服务端也会复核真实格式）。
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp"}
UPLOAD_TIMEOUT = 120_000


class Client(QObject):
    def __init__(self, port, token, parent):
        super().__init__(parent)
        self.base = f"http://127.0.0.1:{port}"
        self.token = QByteArray(f"Bearer {token}".encode())
        self.manager = QNetworkAccessManager(self)
        self.manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        # PySide6 在 multipart 请求上必须自己保活：只靠 reply 的父子关系会在
        # 事件循环里被 GC 掉，实测触发 access violation（上传卡死/崩溃）。
        self._inflight = set()

    def _request(self, path, timeout=8000):
        request = QNetworkRequest(QUrl(self.base + path))
        request.setRawHeader(QByteArray(b"Authorization"), self.token)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        request.setTransferTimeout(timeout)
        return request

    @staticmethod
    def _answer(reply, callback, png=False):
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

    def request(self, path, callback, *, payload=None, post=False, png=False, key=None, delete=False):
        if not path.startswith("/api/v1/"):
            callback(None, "接口路径无效")
            return
        request = self._request(path)
        if key:
            request.setRawHeader(QByteArray(b"Idempotency-Key"), QByteArray(key.encode("ascii")))
        if post:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self.manager.post(request, QByteArray(json.dumps(payload or {}, ensure_ascii=False).encode()))
        elif delete:
            reply = self.manager.deleteResource(request)
        else:
            reply = self.manager.get(request)
        reply.finished.connect(lambda: self._answer(reply, callback, png))

    def upload_image(self, content, filename, callback):
        """POST /api/v1/images（multipart）；成功后回调 {"name": 引用令牌}。"""
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix not in CONTENT_TYPES:
            callback(None, "参考图仅支持 PNG、JPEG 或 WebP")
            return
        try:
            request = self._request("/api/v1/images", UPLOAD_TIMEOUT)
            multipart = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
            part = QHttpPart()
            # 文件名固定为 ASCII：中文文件名进 multipart 头既多余又容易编码出错。
            part.setHeader(QNetworkRequest.KnownHeaders.ContentDispositionHeader,
                           QByteArray(f'form-data; name="image"; filename="reference{suffix}"'.encode("ascii")))
            part.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, CONTENT_TYPES[suffix])
            part.setBody(QByteArray(bytes(content)))
            multipart.append(part)
            reply = self.manager.post(request, multipart)
        except (TypeError, ValueError, OSError) as exc:
            callback(None, f"参考图读取失败：{exc}")
            return
        self._inflight.add((reply, multipart))

        def finished():
            self._inflight.discard((reply, multipart))
            self._answer(reply, callback)

        reply.finished.connect(finished)

    def upload_reference(self, path, callback):
        """从磁盘读取后上传（拖入/选择本地文件的最短路径）。"""
        try:
            content = Path(path).read_bytes()
        except OSError as exc:
            callback(None, f"无法读取此文件：{exc}")
            return
        self.upload_image(content, Path(path).name, callback)
