# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "python-socketio[client]", "httpx", "psycopg[binary]"]
# ///
"""Bolha da conversa: alvo do clique, citação e cartão de voz.

    uv run t_conversa.py

Sem rede e sem instância: as amostras saem do ffmpeg e de dicionários montados
aqui, com a mesma forma que a Evolution entrega.

Existe porque clicar no VAZIO ao lado da bolha abria a imagem: o item do
QListView ocupa a largura toda do viewport e não havia hit-test nenhum.
"""

import base64
import pathlib
import subprocess
import sys
import tempfile

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QListView

app = QApplication([])
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import api  # noqa: E402
import main  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="t_conversa_"))
subprocess.run(
    ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
     "-i", "testsrc=size=240x240:rate=1:duration=1", "-frames:v", "1", str(TMP / "q.jpg")],
    check=True,
)
JPEG = base64.b64encode((TMP / "q.jpg").read_bytes()).decode()
ONDA = bytes(range(0, 128, 2))  # 64 amostras, 0..126 (o WhatsApp manda 0..100)

main.P.update(main.PALETAS["whatsapp escuro"])
ok = 0


def audio(seg: int = 2, onda: object = ONDA, ptt: bool = True) -> dict:
    return {
        "key": {"id": "a1", "fromMe": False, "remoteJid": "5581@s.whatsapp.net"},
        "messageType": "audioMessage", "messageTimestamp": 1788983131,
        "message": {"audioMessage": {"seconds": seg, "ptt": ptt, "waveform": onda}},
    }


def imagem() -> dict:
    return {
        "key": {"id": "i1", "fromMe": False, "remoteJid": "5581@s.whatsapp.net"},
        "messageType": "imageMessage", "messageTimestamp": 1788983131,
        "message": {"imageMessage": {"jpegThumbnail": JPEG, "width": 240, "height": 240}},
        "_arquivo": str(TMP / "q.jpg"),
    }


# ---------- 1. citacao de audio: microfone + DURACAO (o Web nao escreve "Áudio")
resposta = audio()
resposta["contextInfo"] = {
    "stanzaId": "ORIGINAL42", "participant": "5581@s.whatsapp.net",
    "quotedMessage": {"audioMessage": {"seconds": 69, "ptt": True}},
}
autor, corpo, _thumb = main.citacao_de(resposta)
assert corpo == "🎤 1:09", f"citação de áudio saiu {corpo!r}, esperava '🎤 1:09'"
print(f"ok  citação de áudio mostra duração: {corpo!r} (era '🎤 Áudio')")
ok += 1

# o Web escreve "Você" quando a mensagem citada e' minha — inclusive em grupo,
# onde o participant e' o meu @lid, aprendido das minhas proprias mensagens
assert autor != "Você", "o autor da citada nao e' meu neste caso"
main.aprender_eu([{"key": {"fromMe": True, "participant": "5581@s.whatsapp.net"}}])
autor2, _c, _t = main.citacao_de(resposta)
assert autor2 == "Você", f"citação da minha mensagem saiu {autor2!r}, esperava 'Você'"
print("ok  citação da minha própria mensagem diz 'Você' (@lid aprendido)")
ok += 1

assert main.id_citado(resposta) == "ORIGINAL42", "stanzaId não chega para o pulo"
assert main.id_citado(audio()) == "", "mensagem sem citação devolveu alvo"
print("ok  id da citada sai do stanzaId — é o alvo do pulo")
ok += 1

# ---------- 2. onda que o WhatsApp ja' manda pronta (base64 pela REST, bytes pelo socket)
por_rest = main.onda_do_payload(base64.b64encode(ONDA).decode())
por_socket = main.onda_do_payload(ONDA)
assert por_rest == por_socket, "REST e socket dão ondas diferentes"
assert len(por_rest) == 64, f"esperava 64 amostras, veio {len(por_rest)}"
assert all(0.0 <= v <= 1.0 for v in por_rest), "amostra fora de 0..1"
assert api.detalhes(audio())["onda"] is not None, "detalhes() não expôs a onda"
print(f"ok  onda vem pronta no payload: {len(por_rest)} amostras, sem ffmpeg")
ok += 1

# ---------- 3. o clique so' vale EM CIMA da midia
vista = QListView()
modelo = main.Mensagens()
modelo.abrir("5581@s.whatsapp.net", [imagem()], 1)
vista.setModel(modelo)
d = main.Balao()
vista.setItemDelegate(d)
vista.resize(900, 400)
vista.show()
app.processEvents()

ix = modelo.index(0)
opt = main.QStyleOptionViewItem()
opt.rect = vista.visualRect(ix)
opt.font = vista.font()
msg, _t, pix, _det, larg, alto, _mt = d._medir(opt, ix)
caixa, y = d.geometria_do_item(opt, ix, msg, larg, alto)
area = d._area_midia(caixa, y, pix)

assert area.contains(area.center()), "o centro da imagem não é alvo"
fora_x = QPoint(vista.viewport().width() - 5, area.center().y())
assert not area.contains(fora_x), "clique no VAZIO, no mesmo y, ainda abre a imagem"
fora_y = QPoint(area.center().x(), opt.rect.top() + 1)
assert not area.contains(fora_y), "clique acima da imagem ainda abre a imagem"
print(f"ok  alvo da imagem é só ela: {area.width()}x{area.height()} de "
      f"{vista.viewport().width()}px de linha")
ok += 1

# a fiacao velha ligava o clique da LINHA INTEIRA ao visualizador
fonte = pathlib.Path("main.py").read_text()
assert "conversa.clicked.connect" not in fonte, "o clique da linha inteira voltou"
print("ok  o sinal `clicked` da linha inteira não está mais ligado")
ok += 1

# ---------- 4. a faixa da citacao e' clicavel e nao invade a midia
modelo2 = main.Mensagens()
citando = imagem()
citando["contextInfo"] = {
    "stanzaId": "ORIGINAL42", "participant": "5581@s.whatsapp.net",
    "quotedMessage": {"conversation": "mensagem original"},
}
modelo2.abrir("5581@s.whatsapp.net", [citando], 1)
vista.setModel(modelo2)
app.processEvents()
ix2 = modelo2.index(0)
opt.rect = vista.visualRect(ix2)
msg2, _t, pix2, _d2, larg2, alto2, _m2 = d._medir(opt, ix2)
caixa2, y2 = d.geometria_do_item(opt, ix2, msg2, larg2, alto2)
faixa = d._area_citacao(caixa2, y2)
midia2 = d._area_midia(caixa2, y2, pix2)
assert faixa.height() > 0 and caixa2.contains(faixa.center()), "faixa da citação fora da bolha"
assert not faixa.intersects(midia2), "citação e mídia se sobrepõem — o clique seria ambíguo"
assert faixa.bottom() <= midia2.top(), "a citação não está ACIMA da mídia"
print("ok  faixa da citação clicável, acima da mídia e sem sobrepor")
ok += 1

# ---------- 5. cartao de voz: retrato com selo de microfone
modelo3 = main.Mensagens()
modelo3.abrir("5581@s.whatsapp.net", [audio()], 1)
vista.setModel(modelo3)
d.contato = ("Ana Teste", "5581@s.whatsapp.net", "")
app.processEvents()
ix3 = modelo3.index(0)
opt.rect = vista.visualRect(ix3)
msg3, _t, _p3, det3, larg3, alto3, _m3 = d._medir(opt, ix3)
caixa3, y3 = d.geometria_do_item(opt, ix3, msg3, larg3, alto3)
botao, onda_r, retrato = d.geometria_audio(caixa3, y3)
assert caixa3.contains(retrato), "o retrato caiu fora da bolha"
assert not retrato.intersects(onda_r), "o retrato está em cima da onda"
assert onda_r.width() > 60, f"a onda ficou espremida: {onda_r.width()}px"
print(f"ok  cartão de voz: onda {onda_r.width()}px + retrato {retrato.width()}px, sem colidir")
ok += 1

img = vista.grab().toImage()
img.save("/tmp/voz.png")
def cores(r):
    """Cores distintas dentro do retangulo — fundo chapado da' 1, desenho da' varias."""
    return {img.pixelColor(x, y).rgb()
            for x in range(r.left(), r.right(), 2)
            for y in range(r.top(), r.bottom(), 2)}


# o centro do retrato sao as INICIAIS brancas; o que prova desenho e' a variedade
assert len(cores(retrato)) > 3, "nada foi desenhado onde o retrato deveria estar"
lado_selo = max(main.ESCALA.e(13), retrato.width() // 3)
selo = main.QRect(retrato.left(), retrato.bottom() - lado_selo + 1, lado_selo, lado_selo)
accent = main.QColor(main.P["accent"])
perto = [c for c in cores(selo)
         if abs(main.QColor(c).green() - accent.green()) < 60
         and main.QColor(c).green() > main.QColor(c).red()]
assert perto, f"nenhum pixel verde no selo de microfone (cores: {len(cores(selo))})"
print(f"ok  retrato desenhado ({len(cores(retrato))} cores) e selo de microfone verde")
ok += 1

print(f"\n{ok}/{ok} passaram · imagem em {TMP / 'voz.png'}")
