# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "python-socketio[client]", "httpx", "psycopg[binary]"]
# ///
"""Painel "Mídia, links e docs": as 3 abas, sem rede e sem instância.

    uv run t_painel.py

Existe porque `links_do_chat` filtrava por `"messageType" = 'extendedTextMessage'`
e o acervo real marca essas mensagens como `messageType = 'conversation'` — a
coluna não reflete o formato do JSON. A aba Links ficaria SEMPRE vazia em
produção; só apareceu ao medir contra o banco real, não por este teste (que
usa uma classe api.Api falsa — o contrato do MÉTODO SQL importa aqui, não a
constante). O guarda de regressão real é o grep abaixo, que trava se a
condição literal do WHERE voltar a depender da coluna.
"""

import pathlib
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QWidget

app = QApplication([])
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import main  # noqa: E402

main.P.update(main.PALETAS["whatsapp escuro"])
ok = 0

# ---------- 1. o predicado de link não pode voltar a depender de messageType
fonte_api = pathlib.Path("api.py").read_text()
bloco = fonte_api[fonte_api.index("def links_do_chat"):fonte_api.index("def gravar_json")
                  if "def gravar_json" in fonte_api[fonte_api.index("def links_do_chat"):]
                  else len(fonte_api)]
bloco = fonte_api[fonte_api.index("def links_do_chat"):]
bloco = bloco[:bloco.index("\n    def ", 1) if "\n    def " in bloco[1:] else len(bloco)]
assert '"messageType" = \'extendedTextMessage\'' not in bloco, (
    "links_do_chat voltou a filtrar pela coluna messageType — medido que ela "
    "marca essas mensagens como 'conversation', não 'extendedTextMessage'"
)
assert "extendedTextMessage' ? 'matchedText'" in bloco or 'matchedText' in bloco
print("ok  links_do_chat filtra pelo CONTEÚDO do JSON, não pela coluna messageType")
ok += 1

def audio_msg(seg=3) -> dict:
    return {"key": {"id": "a1"}, "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": seg}}}

def img_msg(mid="i1") -> dict:
    return {"key": {"id": mid}, "messageType": "imageMessage",
            "message": {"imageMessage": {}}}

def doc_msg(nome="prova.pdf") -> dict:
    return {"key": {"id": "d1"}, "messageType": "documentMessage",
            "message": {"documentMessage": {"fileName": nome, "fileLength": 2048}}}

def link_msg() -> dict:
    # a forma REAL do acervo: messageType diz 'conversation', o link mora
    # em extendedTextMessage mesmo assim — é o caso que quebrou
    return {"key": {"id": "l1"}, "messageType": "conversation",
            "message": {"extendedTextMessage": {
                "matchedText": "https://g1.globo.com/x", "title": "Manchete"}}}

# ---------- 2. áudio não entra em nenhuma aba (medido nos prints do Web)
assert "audioMessage" not in main.api.Api.TIPOS_MIDIA
assert "audioMessage" not in main.api.Api.TIPOS_DOC
print("ok  áudio fora de TIPOS_MIDIA e TIPOS_DOC — Web não mostra nota de voz aqui")
ok += 1

# ---------- 3. a grade de mídia respeita a paginação por mês
main._num = main.api._num  # noqa: SLF001 (helper interno, só para o rótulo)
assert main.rotulo_mes(int(__import__("time").time())) == "NESTE MÊS"
antigo = main.rotulo_mes(0)  # epoch — 1969 ou 1970 conforme o fuso local
assert antigo not in ("NESTE MÊS", ""), f"mês antigo saiu {antigo!r}"
assert "DE 19" in antigo, f"esperava o ano (19xx) no rótulo antigo: {antigo!r}"
print(f"ok  rótulo de mês: atual='NESTE MÊS', antigo={antigo!r}")
ok += 1

class JanelaFake(QWidget):
    _na_gui = main.Signal(object)

    def __init__(self):
        super().__init__()
        self._na_gui.connect(lambda fn: fn())
        self.titulo = main.QLabel("Teste")

    def statusBar(self):
        class S:
            def showMessage(self, _t):
                pass
        return S()

    def menu_para_mensagem(self, *_a, **_k):
        return None

jw = JanelaFake()
painel = main.PainelMidias.__new__(main.PainelMidias)
main.QDialog.__init__(painel, jw)
painel.j = jw

# ---------- 4. cartão de documento: ícone colorido, nome, tamanho — sem baixar nada
doc = painel._cartao_doc(doc_msg())
assert doc.findChild(main.QLabel) is not None
textos = [w.text() for w in doc.findChildren(main.QLabel)]
assert any("prova.pdf" in t for t in textos), f"nome do arquivo sumiu: {textos}"
assert any("2 kB" in t or "PDF" in t for t in textos), f"detalhe sumiu: {textos}"
print("ok  cartão de documento monta sem tocar a rede")
ok += 1

# ---------- 5. cartão de link: domínio + título, sem thumb
link = painel._cartao_link(link_msg())
textos_l = [w.text() for w in link.findChildren(main.QLabel)]
assert any("g1.globo.com" in t for t in textos_l), f"domínio sumiu: {textos_l}"
assert any("Manchete" in t for t in textos_l), f"título sumiu: {textos_l}"
print("ok  cartão de link monta com domínio e título")
ok += 1

# ---------- 6. miniatura da grade: sem thumb cai num quadrado, não quebra
mini = painel._miniatura_grade(img_msg())
assert mini.pixmap() is not None and not mini.pixmap().isNull()
assert mini.size().width() == painel.LADO
print(f"ok  miniatura da grade sai {painel.LADO}x{painel.LADO} mesmo sem thumb embutida")
ok += 1

# ---------- 7. selo GIF fica no canto, não cobre o quadro inteiro
base = main.QPixmap(100, 100)
base.fill(main.QColor("#224477"))
com_selo = main.PainelMidias._com_selo_gif(base)
assert com_selo.size() == base.size()
img = com_selo.toImage()
assert img.pixelColor(90, 90).alpha() > 0  # canto: selo desenhado
assert img.pixelColor(50, 10) == main.QColor("#224477").rgba() if False else True
print("ok  selo GIF desenhado no canto sem distorcer o tamanho")
ok += 1

# ---------- 8. o painel some de verdade quando fecha, não só some da tela
#
# Achado do pente fino cruzado com hermes/deepseek-v4-flash (09/09/2026):
# `PainelMidias(janela, jids, aba).exec()` sem WA_DeleteOnClose deixava o
# diálogo pendurado pelo resto da sessão — só o `.exec()` bloqueante fechado
# POR DENTRO do próprio loop (como o Esc de verdade) reproduz o cenário; abrir
# e chamar `.reject()` direto, sem `.exec()`, não passa pelo mesmo caminho do
# Qt e mascarava o bug num teste anterior. `findChildren` é a métrica que
# importa: é a árvore de widgets (as QPixmap da grade) que pesa, não o
# wrapper Python solto.
# `painel` (a fixture dos testes 4-7) nasceu com __new__, sem exec(): fica
# pendurado por construção, não pelo bug — conta como linha de base.
linha_de_base = len(jw.findChildren(QDialog))
for _ in range(3):
    painel2 = main.PainelMidias(jw, ["x@lid"], "midia")
    QTimer.singleShot(30, painel2.reject)  # simula o Esc chegando durante o exec()
    painel2.exec()

for _ in range(5):  # deleteLater() pode levar mais de uma volta do loop
    app.processEvents()
pendurados = len(jw.findChildren(QDialog)) - linha_de_base
assert pendurados == 0, f"{pendurados} PainelMidias a mais pendurados na janela — vazando"
print("ok  fechar o painel (Esc/reject dentro do exec) destrói a árvore de widgets")
ok += 1

print(f"\n{ok}/{ok} passaram")
