# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "python-socketio[client]", "httpx", "psycopg[binary]"]
# ///
"""GIF na conversa e no seletor.

    uv run t_gif.py

Não toca a rede nem a instância: as amostras saem do ffmpeg, que o projeto já
usa para a onda do áudio. Roda em `QT_QPA_PLATFORM=offscreen` sem abrir janela.

Existe porque o GIF de bolha vazia custou caro: o `gifPlayback` estava certo no banco e
nenhum GIF renderizava no cliente. Validar o protocolo não é validar o produto.
"""

import base64
import pathlib
import subprocess
import sys
import tempfile

from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QFont, QImage, QMovie, QPainter
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

app = QApplication([])
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import main  # noqa: E402  (depende do QApplication existir antes)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="t_gif_"))


def amostra(nome: str, *extra: str) -> str:
    """1s de vídeo sintético — sem depender do acervo de mídia local."""
    destino = TMP / nome
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x160:rate=10:duration=1", *extra, str(destino)],
        check=True,
    )
    return str(destino)


ok = 0

# ---------- 1. a miniatura embutida chega em DOIS tipos
# A REST devolve base64; o socket.io entrega bytes crus (payload binario, e o
# campo e' um Buffer do protobuf). Tratar so' um dos dois deixava a bolha vazia
# em toda midia que chegava ao vivo.
jpeg = pathlib.Path(amostra("q.jpg", "-frames:v", "1")).read_bytes()
b64 = base64.b64encode(jpeg).decode()
assert not main.thumb_embutida(b64, 100).isNull(), "base64 (REST) quebrou"
assert not main.thumb_embutida(jpeg, 100).isNull(), "bytes crus (socket) dao pixmap nulo"
assert main.thumb_embutida(jpeg, 100).size() == main.thumb_embutida(b64, 100).size()
assert not main.link_pix(jpeg, 200).isNull(), "link_pix nao aceita bytes crus"
print("ok  miniatura aceita base64 (REST) E bytes crus (socket)")
ok += 1

# ---------- 2. o GIF anima na bolha, sozinho
main.ANIMA = main.Animador()
video = amostra("g.mp4", "-pix_fmt", "yuv420p")
quadros: list[str] = []
main.ANIMA.quadro.connect(quadros.append)
main.ANIMA.quadro_de("m1", video)
# o paint e' quem segura o player vivo: sem carimbar, a faxina desliga em 1,5s
pintar = QTimer()
pintar.timeout.connect(lambda: main.ANIMA.quadro_de("m1", video))
pintar.start(200)
QTimer.singleShot(2500, app.quit)
app.exec()
pintar.stop()
pix = main.ANIMA.quadro_de("m1", video)
assert quadros, "nenhum quadro chegou do QVideoSink"
assert pix is not None and not pix.isNull(), "quadro corrente nulo"
assert len(quadros) <= 45, f"throttle furou: {len(quadros)} quadros em 2,5s"
print(f"ok  GIF anima na bolha — {len(quadros)} quadros em 2,5s, {pix.width()}x{pix.height()}")
ok += 1

for i in range(6):
    main.ANIMA.quadro_de(f"x{i}", video)
assert len(main.ANIMA.vivos) <= main.Animador.TETO, f"{len(main.ANIMA.vivos)} players vivos"
print(f"ok  teto de {main.Animador.TETO} players simultâneos respeitado")
ok += 1

for v in main.ANIMA.vivos.values():
    v["visto"] = 0.0
main.ANIMA._podar()
assert not main.ANIMA.vivos, "player ocioso continuou tocando fora da tela"
print("ok  rolou para fora da tela → player desligado")
ok += 1


# ---------- 3. o ▶ nao fica em cima de conteudo em movimento (o Web tambem nao)
def sobreposicao(animando: bool) -> QImage:
    img = QImage(200, 200, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    opt = QStyleOptionViewItem()
    opt.font = QFont()
    main.Balao()._sobrepor_video(p, opt, QRect(0, 0, 200, 200), {"gif": True},
                                 animando=animando)
    p.end()
    return img


parado, rodando = sobreposicao(False), sobreposicao(True)
assert parado.pixelColor(100, 100).alpha() > 0, "o ▶ sumiu do vídeo parado"
assert rodando.pixelColor(100, 100).alpha() == 0, "o ▶ ficou em cima do GIF rodando"
assert rodando.pixelColor(12, 12).alpha() > 0, "o selo GIF sumiu"
print("ok  ▶ só no parado; selo GIF fica nos dois")
ok += 1

# ---------- 4. a grade do seletor anima e para quando deve
prev = amostra("p.gif")
modelo = main.GifModel()
modelo.definir([{"id": "g1"}])
modelo.preview_pronto("g1", prev)
mv = modelo.itens[0].get("_mv")
assert mv is not None, "preview não virou QMovie — a grade fica parada"
assert mv.frameCount() > 1 and mv.state() == QMovie.Running, "QMovie não está rodando"
print(f"ok  preview do seletor anima ({mv.frameCount()} quadros)")
ok += 1

modelo.definir([{"id": "g2"}])
assert mv.state() != QMovie.Running, "busca nova deixou o QMovie velho rodando"
print("ok  busca nova para as animações antigas")
ok += 1

print(f"\n{ok}/{ok} passaram")
