"""网页 SSH 终端的 SSH 端：复用安装模块的连接与主机指纹校验，再打开交互 shell。"""
import socket

from .installer import ParamikoSession


class ParamikoShell(ParamikoSession):
    chan = None

    def open_shell(self, cols=80, rows=24):
        self.client.get_transport().set_keepalive(30)
        self.chan = self.client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        self.chan.settimeout(1.0)

    def read(self):
        """返回 bytes；1 秒内无数据返回 b""；连接结束返回 None。"""
        try:
            d = self.chan.recv(4096)
        except socket.timeout:
            return b""
        return d or None

    def send(self, data):
        self.chan.sendall(data)

    def resize(self, cols, rows):
        self.chan.resize_pty(width=cols, height=rows)

    def close(self):
        try:
            if self.chan:
                self.chan.close()
        finally:
            super().close()
