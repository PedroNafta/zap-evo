# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "python-socketio[client]", "httpx", "psycopg[binary]"]
# ///
"""Mensagem em tempo real chegando na conversa certa.

    QT_QPA_PLATFORM=offscreen uv run t_tempo_real.py

Existe porque mensagem recebida aparecia só na prévia da lista e não na
conversa: desde a migração para @lid toda 1:1 chega como `x@lid` com o número
em `remoteJidAlt`, e a conversa aberta pelo número não casava. E porque a
conversa guardada em memória era restaurada sem o que chegou com ela fechada.
Host apontado para porta morta: nada sai para a rede.
"""

import pathlib
import sys

from PySide6.QtWidgets import QApplication

app = QApplication([])
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import api  # noqa: E402
import main  # noqa: E402

import tempfile  # noqa: E402

api.CACHE = pathlib.Path(tempfile.mkdtemp(prefix="t_tempo_real_"))  # nada no ~/.cache real
main.ANIMA = main.Animador()
cfg = dict(api.load_config(), host="http://127.0.0.1:9", notificar=False)
gravados = []
api.salvar_config = gravados.append  # zoom/divisor gravam o config: nunca o real
api.gravar_json = lambda *a, **k: None  # favoritas, arquivadas... idem
j = main.Janela(cfg)
FONE, LID = "5581999990000@s.whatsapp.net", "99999999999@lid"
ok = 0


def chega(mid, texto, de_mim=False, alt=True, jid=LID):
    chave = {"id": mid, "fromMe": de_mim, "remoteJid": jid}
    if alt:
        chave["remoteJidAlt"] = FONE
    j.mensagem_recebida({"key": chave, "messageType": "conversation",
                         "messageTimestamp": 1795000000, "message": {"conversation": texto}})


def ids():
    return [(m.get("key") or {}).get("id") for m in j.msgs.itens]


# conversa conhecida só pelo número, aberta
j._todos = [{"remoteJid": FONE, "_jids": [FONE], "pushName": "Amigo", "unreadCount": 0}]
j.msgs.abrir(FONE, [], 1)
j.msgs.jids = [FONE]

# 1. chega pelo @lid: entra na conversa e não duplica a linha
chega("m1", "oi")
assert "m1" in ids(), "mensagem pelo @lid não entrou na conversa aberta pelo número"
assert len(j._todos) == 1, f"linha duplicada na lista: {len(j._todos)}"
assert LID in j._todos[0]["_jids"] and LID in j.msgs.jids
ok += 1
print("ok  @lid entra na conversa aberta pelo número, sem linha duplicada")

# 2. eco do meu envio volta como @lid: a bolha pendente adota, não duplica
j._eco_local(FONE, "tudo bem?")
j.mensagem_recebida({"key": {"id": "m2", "fromMe": True, "remoteJid": LID, "remoteJidAlt": FONE},
                     "messageType": "conversation", "messageTimestamp": 1795000000,
                     "status": "PENDING", "message": {"conversation": "tudo bem?"}})
assert ids().count("m2") == 1 and not any(str(x).startswith("local-") for x in ids()), ids()
# o eco vem PENDING e a Evolution não manda update de SERVER_ACK: 🕐 eterno
assert j.msgs.itens[ids().index("m2")]["status"] == "SERVER_ACK"
# e o retorno do POST (também PENDING) não pode rebaixar o que o socket trouxe
eco = j.msgs.itens[ids().index("m2")]
eco["status"] = "DELIVERY_ACK"
j.api.send_text = lambda *a, **k: {"key": eco["key"], "status": "PENDING"}
j._enviar_texto(FONE, "tudo bem?", None, eco)  # _na_gui na mesma thread: direto
assert eco["status"] == "DELIVERY_ACK", eco["status"]
ok += 1
print("ok  eco do envio pelo @lid adota a bolha pendente")

# 3. edição chega: a bolha muda de texto
j.mensagem_recebida({"key": {"id": "e1", "remoteJid": LID, "remoteJidAlt": FONE},
                     "messageType": "protocolMessage", "message": {"protocolMessage": {
                         "type": 14, "key": {"id": "m1"}, "editedMessage": {"conversation": "olá"}}}})
assert api.text_of(j.msgs.itens[ids().index("m1")]) == "olá" and "e1" not in ids()
ok += 1
print("ok  edição recebida troca o texto da bolha")

# 4. sai da conversa, chega mensagem, volta: a cópia em memória não pode ficar velha
j._guardar_no_cache()
assert FONE in j._cache
j.msgs.abrir("outro@s.whatsapp.net", [], 1)
j.msgs.jids = ["outro@s.whatsapp.net"]
chega("m3", "tá aí?")
assert FONE not in j._cache, "conversa guardada sem a mensagem nova seria restaurada"
assert j._todos[0]["unreadCount"] == 1
ok += 1
print("ok  mensagem com a conversa fechada invalida a cópia em memória")

# 5. queda do socket esquece tudo que estava guardado
j._cache["x"] = {"itens": [], "jids": ["x"], "pagina": 1, "paginas": 1, "saiu_em": None}
j.mostrar_conexao("caiu")
assert not j._cache
ok += 1
print("ok  queda do socket esvazia as conversas guardadas")

# 6. duas linhas do mesmo contato, aberta a do número; chega
#    pelo @lid -> as linhas se fundem e a conversa recebe a mensagem
j._todos = [
    {"remoteJid": FONE, "_jids": [FONE], "pushName": "Carla", "unreadCount": 0},
    {"remoteJid": LID, "_jids": [LID], "unreadCount": 2},
]
j.msgs.abrir(FONE, [], 1)
j.msgs.jids = [FONE]
chega("b1", "chegou agora")
assert "b1" in ids(), "contato partido: mensagem não entrou na conversa"
assert len(j._todos) == 1 and set(j._todos[0]["_jids"]) == {FONE, LID}, j._todos
ok += 1
print("ok  contato partido em duas linhas se funde quando a mensagem chega")

# 7. mensagem que chega enquanto a conversa carrega não some quando a carga
#    termina; e carga de conversa que já não está aberta é descartada
velha = {"key": {"id": "v1", "remoteJid": FONE}, "messageType": "conversation",
         "messageTimestamp": 1794000000, "message": {"conversation": "antiga"}}
j._pintar_conversa(FONE, [velha], 1)
assert ids() == ["v1", "b1"], ids()
j._pintar_conversa("outro@s.whatsapp.net", [], 1)
assert ids() == ["v1", "b1"], "carga atrasada de outra conversa pintou por cima"
ok += 1
print("ok  carga da conversa preserva o que chegou no meio e ignora carga atrasada")

# 8. mapa aprendido: minha mensagem pelo @lid (sem Alt) acha a linha do número
api.LIDS[LID] = FONE
j._todos = [{"remoteJid": FONE, "_jids": [FONE], "unreadCount": 0}]
chega("b2", "já vou", de_mim=True, alt=False)
assert len(j._todos) == 1 and LID in j._todos[0]["_jids"]
ok += 1
print("ok  mensagem minha pelo @lid sem Alt acha o contato pelo mapa aprendido")

# ---------- rolagem ao abrir: no fim, sem paginar sozinha
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

pedidas = []
j.api.messages = lambda *a, **k: (pedidas.append(a), ([], 1))[1]
j.resize(1100, 720)
j.show()


def lote(n, de=0, base=1795000000):
    return [{"key": {"id": f"h{i}", "fromMe": i % 3 == 0, "remoteJid": FONE},
             "messageType": "conversation", "messageTimestamp": base + i * 60,
             "message": {"conversation": f"mensagem {i}"}} for i in range(de + n - 1, de - 1, -1)]


def abre(unread):
    j._todos = [{"remoteJid": FONE, "_jids": [FONE], "unreadCount": unread}]
    j.msgs.jids = [FONE]
    j.conversa.verticalScrollBar().setValue(0)
    j._pintar_conversa(FONE, lote(150, de=50), 5)
    QTest.qWait(400)


barra = j.conversa.verticalScrollBar()
abre(0)
assert barra.value() >= barra.maximum() - 40, f"abriu longe do fim: {barra.value()}/{barra.maximum()}"
assert not pedidas, f"abrir pediu página antiga sozinho: {pedidas}"
ok += 1
print("ok  sem não lidas: abre no fim e não pagina sozinho")

# status vem vazio da API: só as últimas `unreadCount` recebidas são não lidas
abre(3)
topo = j.conversa.indexAt(QPoint(0, 0)).row()
assert topo >= 140, f"âncora de não lidas foi para a linha {topo} (dias atrás)"
ok += 1
print("ok  com 3 não lidas: âncora na mais antiga delas, não no começo da janela")

# carregar a página anterior não pode empurrar a tela
barra.setValue(0)
QTest.qWait(50)
antes = j.msgs.itens[j.conversa.indexAt(QPoint(0, 0)).row()]
j._pintar_pagina(FONE, lote(50, de=0))
QTest.qWait(100)
depois = j.msgs.itens[j.conversa.indexAt(QPoint(0, 0)).row()]
assert depois is antes, f"tela pulou: topo era {antes['key']['id']}, virou {depois['key']['id']}"
ok += 1
print("ok  página antiga entra acima sem mexer no que está na tela")

# ação do menu (editar/reagir/apagar) precisa do item DE VERDADE: o
# data(UserRole) do PySide devolve cópia e a bolha não atualizava
assert main.item_de(j.msgs.index(0)) is j.msgs.itens[0]
ok += 1
print("ok  item_de devolve o item do modelo, não a cópia do PySide")

# ---------- botão direito e visualizador: o direito é menu, não "abrir";
# e o menu do visualizador precisa nascer nele (Wayland ancora popup no pai)
from PySide6.QtCore import QPointF, QEvent  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent, QColor  # noqa: E402

jpg = api.CACHE / "foto.jpg"
img = QImage(320, 240, QImage.Format_RGB32)
img.fill(QColor("#3a7"))
img.save(str(jpg))
foto = {"key": {"id": "f1", "fromMe": False, "remoteJid": FONE}, "messageType": "imageMessage",
        "messageTimestamp": 1795000500, "message": {"imageMessage": {"width": 320, "height": 240}},
        "_arquivo": str(jpg)}
j.msgs.abrir(FONE, [foto], 1)
j.msgs.jids = [FONE]
QTest.qWait(100)
ix = j.msgs.index(0)
delegado, caixa, y, pix, _ = j._geometria(ix, foto)
alvo = QPointF(delegado._area_midia(caixa, y, pix).center())
abertos = []
j.abrir_anexo = lambda m: abertos.append(m)


def aperta(botao):
    ev = QMouseEvent(QEvent.MouseButtonPress, alvo, alvo, botao, botao, main.Qt.NoModifier)
    return j.eventFilter(j.conversa.viewport(), ev)


assert not aperta(main.Qt.RightButton), "botão direito foi consumido como clique"
QTest.qWait(50)
assert not abertos, "botão direito abriu o visualizador"
assert aperta(main.Qt.LeftButton)
QTest.qWait(50)
assert abertos, "botão esquerdo deixou de abrir a mídia"
ok += 1
print("ok  botão direito na imagem não abre o visualizador; o esquerdo abre")

v = main.Visualizador([foto], 0, "Carla", j, menu_de=j.menu_para_mensagem)
v.show()
QTest.qWait(50)
pais = []


def fabrica(msg, **kw):  # o menu de verdade, só sem o exec() que bloquearia
    menu = j.menu_para_mensagem(msg, **kw)
    menu.exec = lambda *a: pais.append(menu.parentWidget())
    return menu


v.menu_de = fabrica
v._menu(v.mais.mapToGlobal(v.mais.rect().bottomLeft()))
v.menu_de = j.menu_para_mensagem
assert pais and pais[0] is v, f"menu do visualizador ancorado em {pais}"
ok += 1
print("ok  ⋮ e clique direito do visualizador ancoram o menu nele (Wayland)")

assert v.copiar.isVisible()
QApplication.clipboard().clear()
v.copiar.click()
copiada = QApplication.clipboard().image()
assert not copiada.isNull() and copiada.width() == 320, "⧉ não copiou a imagem"
assert v.copiar.text() == "✓"
ok += 1
print("ok  ⧉ copia a imagem para a área de transferência")
v.close()

# popup de emoji/GIF: contido na própria janela, nunca medido pela tela
# (Wayland não diz onde a janela está; com vários monitores o primário pode ser o errado)
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

pop = QWidget(j, main.Qt.Popup)
pop.resize(420, 380)
j._sobre_o_campo(pop)
caixa = QRect(j.mapToGlobal(main.QPoint(0, 0)), j.size())
assert caixa.contains(QRect(pop.pos(), pop.size())), f"popup fora da janela: {pop.pos()} em {caixa}"
pop.deleteLater()
ok += 1
print("ok  popup de emoji/GIF fica dentro da própria janela")

# ---------- velocidade NA BOLHA, como no WhatsApp Web
wav = api.CACHE / "voz.ogg"
wav.write_bytes(b"0" * 2048)
voz = {"key": {"id": "a1", "fromMe": False, "remoteJid": FONE}, "messageType": "audioMessage",
       "messageTimestamp": 1795000600, "message": {"audioMessage": {"seconds": 9, "ptt": True}},
       "_arquivo": str(wav)}
j.msgs.abrir(FONE, [voz], 1)
j.msgs.jids = [FONE]
QTest.qWait(100)
delegado, caixa, y, _, _ = j._geometria(j.msgs.index(0), voz)
_b, _o, retrato = delegado.geometria_audio(caixa, y)
no_retrato = QPointF(retrato.center())
main.AUDIO.velocidade = 1.0
assert not j._clique_audio(j.msgs.index(0), no_retrato.toPoint(), voz) or main.AUDIO.velocidade == 1.0, \
    "áudio parado: o retrato não é botão de velocidade"
main.AUDIO.em_curso = lambda mid: mid == "a1"  # tocando, sem sair som do teste
assert j._clique_audio(j.msgs.index(0), no_retrato.toPoint(), voz)
assert main.AUDIO.velocidade == 1.5 and main.AUDIO.rotulo_velocidade() == "1,5×"
j._clique_audio(j.msgs.index(0), no_retrato.toPoint(), voz)
j._clique_audio(j.msgs.index(0), no_retrato.toPoint(), voz)
assert main.AUDIO.velocidade == 1.0, "1× → 1,5× → 2× → 1×"
del main.AUDIO.em_curso
ok += 1
print("ok  pílula de velocidade na bolha em curso: 1× → 1,5× → 2× → 1×")

# ---------- gravação no lugar do campo: onda, cancelar, pausar/retomar, enviar
from PySide6.QtCore import QObject, Signal  # noqa: E402


class GravadorFalso(QObject):  # o de verdade abriria o microfone
    pronto = Signal(pathlib.Path)
    nivel = Signal(float)

    def __init__(self):
        super().__init__()
        self.estado, self.log = "parado", []

    def iniciar(self): self.estado = "gravando"; self.log.append("iniciar")
    def gravando(self): return self.estado != "parado"
    def pausado(self): return self.estado == "pausado"
    def pausar(self): self.estado = "pausado"; self.log.append("pausar")
    def retomar(self): self.estado = "gravando"; self.log.append("retomar")
    def parar(self): self.estado = "parado"; self.log.append("parar")
    def cancelar(self): self.estado = "parado"; self.log.append("cancelar")
    def duracao(self): return 7000


g = GravadorFalso()
j._gravador = g
j._gravador.pronto.connect(j._ptt_pronto)
enviados = []
j.api.send_audio = lambda jid, arq: enviados.append((jid, arq))
j.api.presenca = lambda *a, **k: None
j._tentar = lambda fn, *a: fn(*a)

j.alternar_ptt()
barra = j._barra_gravacao
assert not barra.isHidden() and j.pilula.isHidden() and j.b_mic.isHidden(), "campo não virou gravação"
assert barra.tempo.text() == "0:07"
for v in (0.1, 0.8, 0.3):
    g.nivel.emit(v)
assert list(barra.picos) == [0.1, 0.8, 0.3], "onda não recebe o nível do microfone"
barra.b_pausa.click()
assert g.pausado() and barra.b_pausa.text() == "🎤", "⏸ não virou 🎤 (retomar)"
g.nivel.emit(0.9)
assert len(barra.picos) == 3, "pausado, a onda não anda"
barra.b_pausa.click()
assert not g.pausado() and barra.b_pausa.text() == "⏸"
barra.cancelar.emit()
assert g.log[-1] == "cancelar" and not j.pilula.isHidden() and barra.isHidden()
g.pronto.emit(pathlib.Path("/tmp/x.wav"))
QTest.qWait(50)
assert not enviados, "cancelado não pode enviar"

j.alternar_ptt()  # de novo, e troca de conversa no meio
j.msgs.jids = ["outro@s.whatsapp.net"]
j.msgs.jid = "outro@s.whatsapp.net"
barra.enviar.emit()
g.pronto.emit(pathlib.Path("/tmp/y.wav"))
# o envio roda numa thread do POOL: QTest.qWait segura o GIL no PySide6 e a
# thread mal anda — espera liberando o GIL, com prazo
import time  # noqa: E402

prazo = time.time() + 5
while not enviados and time.time() < prazo:
    QApplication.processEvents()
    time.sleep(0.01)
assert enviados and enviados[0][0] == FONE, f"áudio foi para {enviados}"
assert not j.pilula.isHidden() and barra.isHidden()
ok += 1
print("ok  gravação no lugar do campo: onda, ⏸↔🎤, 🗑 descarta, ➤ vai para quem começou")

# ---------- link na bolha abre (antes era só tinta azul)
abertas = []
main.QDesktopServices = type("D", (), {"openUrl": staticmethod(lambda u: abertas.append(u.toString()))})
txt = {"key": {"id": "l1", "fromMe": False, "remoteJid": FONE}, "messageType": "conversation",
       "messageTimestamp": 1795000700, "message": {"conversation": "olha www.exemplo.com/x."}}
j.stack_direita.setCurrentIndex(0)
j.stack_direita.show()
j.msgs.abrir(FONE, [txt], 1)
j.msgs.jids = [FONE]
QTest.qWait(150)
ix = j.msgs.index(0)
rect = j.conversa.visualRect(ix)
deleg = j.conversa.itemDelegate()
canto, doc = deleg.links["l1"]["texto"]
alvo_link = next((rect.topLeft() + canto + main.QPoint(x, y)
                  for y in range(2, int(doc.size().height()), 3)
                  for x in range(0, int(doc.size().width()), 3)
                  if deleg.link_em(rect, txt, rect.topLeft() + canto + main.QPoint(x, y))), None)
assert alvo_link, "nenhum ponto do texto é link"
ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(alvo_link), QPointF(alvo_link),
                 main.Qt.LeftButton, main.Qt.LeftButton, main.Qt.NoModifier)
assert j.eventFilter(j.conversa.viewport(), ev)
assert abertas == ["https://www.exemplo.com/x"], abertas
fora = rect.topLeft() + canto + main.QPoint(2, 2)  # "olha": texto, não link
assert not deleg.link_em(rect, txt, fora)
ok += 1
print("ok  link na bolha abre no navegador (www sem esquema, ponto final fora)")

# ---------- Ctrl +/- escala a interface inteira, não só a fonte
base = j.b_mais.width()
linha_base = j.lista.itemDelegate().ALTURA
j.zoom(+4)
assert j.b_mais.width() > base and j.b_mais.width() == main.ESCALA.e(40), "botão não escalou"
assert j.lista.itemDelegate().ALTURA > linha_base, "linha da lista não escalou"
j.zoom(0)
assert j.b_mais.width() == base
ok += 1
print("ok  Ctrl +/- escala botões e a linha da lista, não só a fonte")

# ---------- 🎤 vira ➤ com texto
falados = []
j.enviar_texto = falados.append
j.campo.setPlainText("oi")
assert j.b_mic.text() == "➤"
j.b_mic.click()
assert falados == ["oi"] and not j.campo.toPlainText() and j.b_mic.text() == "🎤"
ok += 1
print("ok  o botão da direita é ➤ com texto e 🎤 com o campo vazio")

# ---------- tiling: janela estreita mostra um painel por vez
j.msgs.jids = [FONE]
j.resize(600, 700)
QTest.qWait(50)
assert j.painel_esq.isHidden() and not j.stack_direita.isHidden() and not j.b_voltar.isHidden()
j.b_voltar.click()
QTest.qWait(50)
assert not j.painel_esq.isHidden() and j.stack_direita.isHidden(), "‹ não voltou para a lista"
j.resize(1200, 700)
QTest.qWait(50)
assert not j.painel_esq.isHidden() and not j.stack_direita.isHidden() and j.b_voltar.isHidden()
j.alternar_lista()
assert j.painel_esq.isHidden()
j.alternar_lista()
j.divisor.setSizes([333, 800])
j._salvar_divisor()
assert gravados and gravados[-1]["lista_px"] == j.divisor.sizes()[0]
ok += 1
print("ok  tiling: um painel por vez no estreito, ‹ volta, lista recolhe, largura lembrada")

# ---------- janela em segundo plano pede atenção ao WM; título leva as não lidas
alertas = []
QApplication.alert = lambda w, ms=0: alertas.append(w)
j.isActiveWindow = lambda: False
chega("u1", "chegou com a janela atrás")
assert alertas == [j], "WM não foi avisado"
j.isActiveWindow = lambda: True
chega("u2", "com a janela na frente")
assert len(alertas) == 1
j._todos = [{"remoteJid": "x@s.whatsapp.net", "unreadCount": 3}]
j.aplicar_filtros()
assert j.windowTitle().startswith("("), j.windowTitle()
ok += 1
print("ok  janela atrás: alerta de urgência ao WM; título com as não lidas")

# ---------- separador encaixa: coluna de avatares (Telegram) e lista mínima
j.resize(1200, 700)
j._lista_recolhida = False
j._ajustar_modo()
total = sum(j.divisor.sizes())
j.divisor.setSizes([main.ESCALA.e(150), total - main.ESCALA.e(150)])
j._encaixar_lista()
assert j.divisor.sizes()[0] == main.ESCALA.e(j.COLUNA), j.divisor.sizes()
assert j.busca.isHidden() and j.faixa_abas.isHidden(), "coluna ainda mostra busca/abas"
j.divisor.setSizes([main.ESCALA.e(200), total - main.ESCALA.e(200)])
j._encaixar_lista()
assert j.divisor.sizes()[0] == main.ESCALA.e(j.LISTA_MIN) and not j.busca.isHidden()
j.chats.definir([{"remoteJid": FONE, "pushName": "Carla", "unreadCount": 2,
                  "lastMessage": {"message": {"conversation": "oi"}}}])
dica = j.chats.data(j.chats.index(0), main.Qt.ToolTipRole)
assert dica.startswith("Carla"), dica
ok += 1
print("ok  separador encaixa: coluna de avatares <170px, lista mínima até 260px")

# ---------- escala da interface nas configurações, em porcentagem
assert main.ESCALA.porcento(main.Escala.BASE) == 100
c = main.Config(j.cfg, j)
c.escala.setCurrentIndex(c.escala.findData(main.Escala.BASE + 2))
assert c.valores()["escala"] == main.Escala.BASE + 2 and c.escala.currentText() == "114%"
c.close()
ok += 1
print("ok  escala da interface escolhida em % nas configurações")

# ---------- divisor "N mensagens não lidas" antes da primeira não lida
lote3 = lote(30, de=200)
j._todos = [{"remoteJid": FONE, "_jids": [FONE], "unreadCount": 3}]
j.msgs.jids = [FONE]
j._naolidas_ao_abrir = 3
j._pintar_conversa(FONE, lote3, 1)
QTest.qWait(50)
marcadas = [m for m in j.msgs.itens if m.get("_divisor_naolidas")]
assert len(marcadas) == 1 and marcadas[0]["_divisor_naolidas"] == 3, marcadas
ix_div = j.msgs.index(j.msgs.itens.index(marcadas[0]))
alto_com = j.conversa.itemDelegate().sizeHint(j.conversa.viewOptions() if hasattr(j.conversa, "viewOptions") else main.QStyleOptionViewItem(), ix_div).height()
marcadas[0].pop("_divisor_naolidas")
alto_sem = j.conversa.itemDelegate().sizeHint(main.QStyleOptionViewItem(), ix_div).height()
assert alto_com > alto_sem, "a faixa não ganhou altura"
ok += 1
print("ok  faixa 'N mensagens não lidas' antes da primeira não lida")

# ---------- áudio: ⏳ baixando e ⚠ indisponível na própria bolha
mudo_aud = dict(voz, _arquivo=None, key={"id": "a2", "fromMe": False, "remoteJid": FONE})
j.msgs.abrir(FONE, [mudo_aud], 1)
j.msgs.jids = [FONE]
j._buscar_midia = lambda m: None
QTest.qWait(50)
assert j._clique_audio(j.msgs.index(0), main.QPoint(0, 0), mudo_aud)
assert mudo_aud.get("_baixando"), "clique num áudio sem arquivo não marcou ⏳"
j._midia_falhou(mudo_aud)
assert mudo_aud.get("_indisponivel") and not mudo_aud.get("_baixando")
ok += 1
print("ok  áudio sem arquivo: ⏳ na bolha ao baixar, ⚠ se falhar")

# ---------- aviso flutuante com Desfazer; faixa de conexão
chat_a = {"remoteJid": "arq@s.whatsapp.net", "_jids": ["arq@s.whatsapp.net"]}
j._todos = [chat_a]
j.api.arquivar = lambda *a: None
j._arquivar_chat(chat_a)
assert "arq@s.whatsapp.net" in j.arquivadas and not j.aviso.isHidden()
assert not j.aviso.b_desfazer.isHidden()
j.aviso.b_desfazer.click()
assert "arq@s.whatsapp.net" not in j.arquivadas and j.aviso.isHidden(), "Desfazer não desarquivou"
j.mostrar_conexao("caiu")
assert not j.faixa_conexao.isHidden()
j._ressincronizar = lambda: None
j.mostrar_conexao("open")
assert j.faixa_conexao.isHidden()
ok += 1
print("ok  'Conversa arquivada' com Desfazer; faixa amarela enquanto a conexão cai")

# ---------- mensagem que falhou: clicar tenta de novo
falhou = j._eco_local(FONE, "vai de novo")
falhou["status"] = "ERROR"
tentativas = []
j._enviar_texto = lambda jid, texto, citado, prov: tentativas.append((jid, texto))
QTest.qWait(50)
assert j._pode_reenviar(falhou)
j.reenviar(falhou)
QTest.qWait(50)
prazo = time.time() + 5
while not tentativas and time.time() < prazo:
    QApplication.processEvents()
    time.sleep(0.01)
assert tentativas == [(FONE, "vai de novo")] and falhou["status"] == "PENDING", tentativas
ok += 1
print("ok  mensagem que falhou: clicar (ou o menu) tenta enviar de novo")

# ---------- grupo: o nome do autor não corta numa mensagem curta
GRUPO = "123@g.us"
curta = {"key": {"id": "g1", "fromMe": False, "remoteJid": GRUPO, "participant": "77@lid"},
         "pushName": "Fulana de Tal Comprido", "messageType": "conversation",
         "messageTimestamp": 1795000800, "message": {"conversation": "oi"}}
j.msgs.abrir(GRUPO, [curta], 1)
j.msgs.jids = [GRUPO]
QTest.qWait(50)
opt = main.QStyleOptionViewItem()
opt.rect = j.conversa.visualRect(j.msgs.index(0))
opt.font = j.conversa.font()
deleg = j.conversa.itemDelegate()
_m, _t, _p, _d, larg_bolha, _a, _mt = deleg._medir(opt, j.msgs.index(0))
nome_px = main.QFontMetrics(main.fonte(opt.font, -0.5, negrito=True)).horizontalAdvance(
    deleg._nome_autor(curta))
assert larg_bolha - deleg.PAD_H * 2 >= nome_px, f"nome cortado: {larg_bolha} < {nome_px}"
ok += 1
print("ok  grupo: a hora na mesma linha não encolhe a bolha abaixo do nome do autor")

# ---------- linha da lista: hora, badge e pino acompanham o Ctrl +/-
# (33 medidas em px cru ignoravam a escala; no zoom alto a coluna da direita
# saía desalinhada). Guarda de fonte: nenhum px cru dentro de ItemChat.paint.
import re

fonte_main = pathlib.Path(main.__file__).read_text(encoding="utf-8")
trecho_lista = fonte_main.split("class ItemChat")[1].split("def documento(")[0]
crus = re.findall(r"r\.top\(\) \+ \d+|r\.right\(\) - \d+|max\(\d+, r\.width", trecho_lista)
assert not crus, f"medida em px cru na linha da lista: {crus}"
ok += 1
print("ok  linha da lista: hora, badge e pino escalam junto com o zoom")


def render_linha(chat, largura=420):
    """Pinta a linha da lista num QImage e devolve os bytes — comparação estável."""
    j.chats.definir([chat])
    ix = j.chats.index(0)
    opt = main.QStyleOptionViewItem()
    opt.font = j.lista.font()
    opt.rect = main.QRect(0, 0, largura, main.ESCALA.e(72))
    img = QImage(opt.rect.size(), QImage.Format_ARGB32)
    img.fill(0)
    pintor = main.QPainter(img)
    j.lista.itemDelegate().paint(pintor, opt, ix)
    pintor.end()
    return bytes(img.bits())


# ---------- "digitando…" na linha da lista, com a conversa fechada
chat_dig = {"remoteJid": FONE, "_jids": [FONE], "pushName": "Amigo", "unreadCount": 0,
            "lastMessage": {"key": {"id": "z1", "fromMe": False, "remoteJid": FONE},
                            "messageType": "conversation", "messageTimestamp": 1795000000,
                            "message": {"conversation": "mensagem antiga"}}}
j._todos = [chat_dig]
j.msgs.jids = [GRUPO]  # conversa aberta é outra: a lista tem que mostrar sozinha
antes_dig = render_linha(chat_dig)
j.mostrar_presenca(FONE, "composing")
assert (chat_dig.get("_digitando") or ("", 0))[0] == "digitando…", chat_dig.get("_digitando")
depois_dig = render_linha(chat_dig)
assert antes_dig != depois_dig, "a linha da lista não mostrou 'digitando…'"
chat_dig["_digitando"] = None
assert render_linha(chat_dig) == antes_dig, "a prévia não voltou depois do 'digitando…'"
ok += 1
print("ok  'digitando…' aparece na própria linha da lista, com a conversa fechada")

# ---------- duplo clique na mensagem responde (Telegram Desktop)
j.msgs.abrir(FONE, lote(3, de=300), 1)
j.msgs.jids = [FONE]
j._buscar_midia = lambda msg: None
QTest.qWait(60)
respondidas = []
j.responder = lambda msg: respondidas.append(msg)
retangulo = j.conversa.visualRect(j.msgs.index(1))
assert retangulo.isValid() and not retangulo.isEmpty(), retangulo
QTest.mouseDClick(j.conversa.viewport(), main.Qt.LeftButton, pos=retangulo.center())
QTest.qWait(30)
assert respondidas == [j.msgs.itens[1]], f"duplo clique não respondeu: {respondidas}"
respondidas.clear()
eco_local = j._eco_local(FONE, "provisória")
ix_eco = j.msgs.index(j.msgs.itens.index(eco_local))
QTest.mouseDClick(j.conversa.viewport(), main.Qt.LeftButton,
                  pos=j.conversa.visualRect(ix_eco).center())
QTest.qWait(30)
assert not respondidas, "duplo clique respondeu a uma bolha provisória"
ok += 1
print("ok  duplo clique na mensagem responde; bolha provisória fica de fora")

# ---------- grupo: a foto de cada participante na bolha
original_midia = main.MIDIA


class PoolImediato:
    """MIDIA que roda na mesma thread — sem espera pelo pool no teste."""

    @classmethod
    def submit(cls, fn):
        fn()


main.MIDIA = PoolImediato
pathtmp = api.CACHE / "autor-do-grupo.jpg"
try:
    j.api.avatar_file = lambda chat: pathtmp
    no_grupo = {"key": {"id": "g2", "fromMe": False, "remoteJid": GRUPO, "participant": "88@lid"},
                "pushName": "Fulano", "messageType": "conversation",
                "messageTimestamp": 1795000900, "message": {"conversation": "oi"}}
    j.msgs.abrir(GRUPO, [no_grupo], 1)
    j.msgs.jids = [GRUPO]
    j._buscar_midia = lambda msg: None
    j._fotos_de_autores([no_grupo])
    assert no_grupo.get("_foto_autor") == str(pathtmp), no_grupo.get("_foto_autor")
    assert j._fotos_autor["88@lid"] == str(pathtmp)
    j._fotos_de_autores([no_grupo])  # segunda mensagem do mesmo autor: sem novo pedido
    assert no_grupo.get("_foto_autor") == str(pathtmp)
finally:
    main.MIDIA = original_midia
ok += 1
print("ok  grupo: a foto do participante chega na bolha e não é re-pedida")

# ---------- leitura local: sem recibo ao WhatsApp e sem repetir as não lidas
j.lidas.clear()
recibos = []
j.api.marcar_lida = lambda keys: recibos.append(keys)
j._todos = [{"remoteJid": FONE, "_jids": [FONE], "unreadCount": 3}]
j.msgs.jids = [FONE]
j._naolidas_ao_abrir = 3
j._pintar_conversa(FONE, lote(150, de=50), 5)
QTest.qWait(400)
assert not recibos, f"a abertura mandou recibo de leitura ao WhatsApp: {recibos}"
chat_lido = j._todos[0]
assert chat_lido.get("unreadCount") == 0, chat_lido.get("unreadCount")
ts_lido = max(m["messageTimestamp"] for m in j.msgs.itens)
assert j.lidas[FONE] >= ts_lido, (j.lidas, ts_lido)
# o servidor continua devolvendo as não lidas dele: a lista não pode repetir
chat_lido["lastMessage"] = {"key": {"id": "h302", "remoteJid": FONE},
                            "messageTimestamp": ts_lido,
                            "messageType": "conversation", "message": {"conversation": "x"}}
j.sincronizar_chats([{"remoteJid": FONE, "unreadCount": 3}])
assert chat_lido["unreadCount"] == 0, f"não lida repetida: {chat_lido['unreadCount']}"
# mensagem nova (timestamp maior) volta a contar normalmente
chat_lido["lastMessage"]["messageTimestamp"] = ts_lido + 600
j.sincronizar_chats([{"remoteJid": FONE, "unreadCount": 2}])
assert chat_lido["unreadCount"] == 2, chat_lido["unreadCount"]
ok += 1
print("ok  leitura é local: nenhum recibo ao WhatsApp e a não lida não se repete")

# ---------- menu da lista: lida e não lida são um ou outro
com_naolidas = dict(chat_lido, unreadCount=4)
menu_lida = j.menu_para_chat(com_naolidas)
assert menu_lida is not None
rotulos = [a.text() for a in menu_lida.actions()]
assert "✓  Marcar como lida" in rotulos, rotulos
assert "✉  Marcar como não lida" not in rotulos, rotulos
sem_naolidas = dict(chat_lido, unreadCount=0)
menu_nao = j.menu_para_chat(sem_naolidas)
assert menu_nao is not None
rotulos = [a.text() for a in menu_nao.actions()]
assert "✉  Marcar como não lida" in rotulos, rotulos
assert "✓  Marcar como lida" not in rotulos, rotulos
ok += 1
print("ok  menu da lista oferece marcar como lida OU como não lida, nunca as duas")

# ---------- marcar como lida pelo menu: zera local, sem tocar no servidor
antes_recibos = len(recibos)
j.marcar_lida(com_naolidas)
assert com_naolidas["unreadCount"] == 0, com_naolidas["unreadCount"]
assert j.lidas[FONE] >= float(com_naolidas["lastMessage"]["messageTimestamp"])
assert len(recibos) == antes_recibos, "marcar como lida mandou recibo ao WhatsApp"
menu_lida = j.menu_para_chat(com_naolidas)
assert menu_lida is not None
rotulos = [a.text() for a in menu_lida.actions()]
assert "✉  Marcar como não lida" in rotulos, rotulos
# e o inverso volta a oferecer "lida"
j.api.marcar_nao_lida = lambda chat: {}
sem = dict(com_naolidas, unreadCount=1)
j.marcar_nao_lida(sem)
QTest.qWait(50)
assert FONE not in j.lidas, j.lidas
menu_inverso = j.menu_para_chat(sem)
assert menu_inverso is not None
rotulos = [a.text() for a in menu_inverso.actions()]
assert "✓  Marcar como lida" in rotulos, rotulos
ok += 1
print("ok  'Marcar como lida' zera local sem recibo; 'não lida' devolve o outro lado")

# ---------- abrir a conversa já deixa o cursor no campo de escrever
j._carregar_conversa = lambda jid: None  # nada de rede
j.chats.definir([{"remoteJid": FONE, "_jids": [FONE], "unreadCount": 0,
                  "pushName": "Amigo"}])
j.lista.setFocus()
QTest.qWait(40)
assert not j.campo.hasFocus(), "pré-condição: o foco deveria estar na lista"
j.abrir_chat(j.chats.index(0))
QTest.qWait(120)
# hasFocus() exige janela ativa; no offscreen o aviso pode estar na frente
# da janela, então a checagem é quem É o widget de foco
assert j.focusWidget() is j.campo, (
    f"foco ficou em {type(j.focusWidget()).__name__}, não no campo de escrever")
ok += 1
print("ok  abrir a conversa leva o cursor direto para o campo de escrever")

# ---------- selecionar trecho na bolha: arrastar, realçar, copiar
msg_txt = {"key": {"id": "s1", "fromMe": False, "remoteJid": FONE},
           "messageType": "conversation", "messageTimestamp": 1795001000,
           "message": {"conversation": "primeira parte do texto e a segunda parte"}}
j.msgs.abrir(FONE, [msg_txt], 1)
j.msgs.jids = [FONE]
j.msgs.jid = FONE
QTest.qWait(150)
deleg = j.conversa.itemDelegate()
ix_txt = j.msgs.index(0)
rect_txt = j.conversa.visualRect(ix_txt)
canto, doc_txt = deleg.links["s1"]["texto"]
inteiro = doc_txt.toPlainText()
ponto_letra = next(
    (rect_txt.topLeft() + canto + main.QPoint(x, y)
     for y in range(2, int(doc_txt.size().height()) + 1, 2)
     for x in range(2, int(doc_txt.size().width()) + 1, 2)
     if (pos := doc_txt.documentLayout().hitTest(
         QPointF(main.QPoint(x, y)), main.Qt.FuzzyHit)) > 0),
    None,
)
assert ponto_letra is not None, "nenhum ponto do texto reconhecido pelo hitTest"
assert j._comecar_selecao(ix_txt, ponto_letra, msg_txt), \
    "arrastar sobre o texto não abriu a seleção"
deleg.selecao = ("s1", 0, 8)
assert deleg.trecho_selecionado(msg_txt) == inteiro[:8], deleg.trecho_selecionado(msg_txt)
main.QApplication.clipboard().clear()
assert j.copiar_selecao() is True, "Ctrl+C não copiou o trecho"
assert main.QApplication.clipboard().text() == inteiro[:8], \
    main.QApplication.clipboard().text()
ok += 1
print("ok  arrastar sobre a bolha seleciona o trecho e Ctrl+C copia")


def render_bolha(indice):
    opt = main.QStyleOptionViewItem()
    opt.rect = j.conversa.visualRect(j.msgs.index(indice))
    opt.font = j.conversa.font()
    img = QImage(opt.rect.size(), QImage.Format_ARGB32)
    img.fill(0)
    pintor = main.QPainter(img)
    j.conversa.itemDelegate().paint(pintor, opt, j.msgs.index(indice))
    pintor.end()
    return bytes(img.bits())


deleg.selecao = ("s1", 0, 8)
com_selecao = render_bolha(0)
deleg.selecao = None
sem_selecao = render_bolha(0)
assert com_selecao != sem_selecao, "o trecho selecionado não é realçado na bolha"
deleg.selecao = ("s1", 0, 8)
menu_sel = j.menu_para_mensagem(msg_txt)
rotulos_sel = [a.text() for a in menu_sel.actions()]
assert any("Copiar trecho" in r for r in rotulos_sel), rotulos_sel
j._limpar_selecao()
assert deleg.selecao is None, "soltar a seleção não limpou o realce"
ok += 1
print("ok  trecho selecionado é realçado, e o menu da bolha oferece copiar")

# ---------- segurar o 🎤 grava; soltar depois envia (clique curto continua gravando)
j.campo.clear()
g.estado, g.log = "parado", []
j._mic_pressionado()
assert g.gravando(), "segurar o 🎤 não começou a gravar"
j._mic_desde -= (j.MIC_SEGURAR + 0.2)  # finge que segurou o tempo suficiente
paradas = g.log.count("parar")
j._mic_solto()
assert g.log.count("parar") == paradas + 1, "soltar depois de segurar não enviou"
j._fim_gravacao()
j._cancelar_gravacao()
g.estado, g.log = "parado", []
j._mic_pressionado()
j._mic_solto()  # soltou na hora
assert g.gravando() and "parar" not in g.log, "clique curto devia continuar gravando"
j._cancelar_gravacao()
ok += 1
print("ok  segurar o 🎤 grava e soltar envia; clique curto mantém gravando")

# ---------- pacote de ícones: troca sem reiniciar, emoji segue padrão
import icones as pacotes_de_icones  # noqa: E402

for pac in ("fluent-phosphor", "icons8-color", "icons8-fluency"):
    faltando = [s for s in pacotes_de_icones.SLOTS
                if pacotes_de_icones.icone(pac, s) is None
                or pacotes_de_icones.icone(pac, s).isNull()]
    assert not faltando, f"{pac} sem desenho em: {faltando}"
assert pacotes_de_icones.normalizar("nao-existe") == "emoji", "pacote inválido devia cair no emoji"
ok += 1
print("ok  os 25 slots existem nos três pacotes, e pacote inválido cai em emoji")

j.pacote_icones = "emoji"
j.aplicar_pacote_icones()
assert j.b_mic.text() == "🎤" and j.b_mic.icon().isNull(), "no pacote emoji o botão devia ficar como antes"
j.campo.setPlainText("texto qualquer")
QTest.qWait(30)
assert j.b_mic.text() == "➤", j.b_mic.text()
j.pacote_icones = "icons8-color"
j.aplicar_pacote_icones()
assert j.b_mic.text() == "" and not j.b_mic.icon().isNull(), "com texto o botão devia virar ➤ desenhado"
j.campo.clear()
QTest.qWait(30)
assert j.b_mic.text() == "" and not j.b_mic.icon().isNull(), "com campo vazio o desenho devia ser o microfone"
ok += 1
print("ok  trocar de pacote troca o desenho do microfone/enviar sem reiniciar")

menu_iconado = j.menu_para_chat({"remoteJid": FONE, "unreadCount": 0})
assert menu_iconado is not None
rotulos = [a.text() for a in menu_iconado.actions() if a.text()]
assert any("Fixar no topo" in r for r in rotulos), rotulos
assert not any("📌" in r for r in rotulos), "com pacote de ícones o rótulo não leva emoji"
j.pacote_icones = "emoji"
menu_emoji = j.menu_para_chat({"remoteJid": FONE, "unreadCount": 0})
assert any("📌" in a.text() for a in menu_emoji.actions()), "no pacote emoji o rótulo volta a ter emoji"
ok += 1
print("ok  menu troca emoji por ícone (e volta) conforme o pacote")

cfg_icones = dict(cfg, pacote="x")
dlg_icones = main.Config(cfg_icones, j)
assert dlg_icones.pacote_icones.count() == len(pacotes_de_icones.PACOTES), dlg_icones.pacote_icones.count()
dlg_icones.pacote_icones.setCurrentIndex(dlg_icones.pacote_icones.findData("icons8-fluency"))
assert dlg_icones.valores()["icones"] == "icons8-fluency", dlg_icones.valores().get("icones")
ok += 1
print("ok  a configuração oferece os 4 pacotes e grava a escolha")

print(f"\n{ok}/45 passaram")
main.POOL.shutdown(wait=False, cancel_futures=True)
