"""Rede e disco. Sem Qt de propósito — dá para testar tudo daqui no terminal."""

from __future__ import annotations

import functools
import base64
import json
import time
from datetime import datetime
import mimetypes
import os
import pathlib
import re
import subprocess
import tomllib

import httpx

CONFIG = pathlib.Path.home() / ".config/evolution-client/config.toml"
# ponytail: acervo, nao cache. ~/.cache/ pode ser limpo pelo sistema e a midia
# antiga expira no CDN do WhatsApp — o que sumir daqui nao volta.
MEDIA = pathlib.Path.home() / ".local/share/evolution-client/media"
FOTOS = pathlib.Path.home() / ".local/share/evolution-client/avatars"
FIXADOS = CONFIG.parent / "fixados.json"
RECENTES_EMOJI = CONFIG.parent / "emojis_recentes.json"
HD_ENVIADAS = CONFIG.parent / "hd_enviadas.json"
ARQUIVADAS = CONFIG.parent / "arquivadas.json"
GIFS = pathlib.Path.home() / ".local/share/evolution-client/gifs"
APELIDOS = CONFIG.parent / "apelidos.json"
OCULTOS = CONFIG.parent / "ocultos.json"
SILENCIADOS = CONFIG.parent / "silenciados.json"
FAVORITAS = CONFIG.parent / "favoritas.json"
# ponytail: aqui SIM e' ~/.cache/ — tudo isto se recria sozinho a partir do
# servidor. E' o oposto da midia, que e' acervo e mora em ~/.local/share.
CACHE = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")
) / "evolution-client"

# Quanto de conversa entra na memória ao abrir. O resto vem ao rolar para cima,
# como no WhatsApp: recente na mão, histórico sob demanda.
#
# Medido nesta máquina: 548 mensagens reais = 680 KB de JSON = +3 MB de RSS, e a
# curva de render é plana (60 msgs -> 188 MB, 600 msgs -> 190 MB) porque
# QListView+delegate pinta só as linhas visíveis, não um widget por item. Por
# isso a janela vai direto para o modelo: separar "renderizado" de "em cache"
# economizaria 2 MB e custaria uma camada de estado inteira.
JANELA = 72 * 3600
# Teto de carga inicial. Uma conversa muito movimentada pode ter mais que isto
# em 72h (medi 447 numa real); aí a janela para aqui e o resto vem ao rolar,
# igual ao histórico antigo. Fica abaixo do TETO do modelo (500) de propósito.
TETO_JANELA = 400
# Conversa fechada há mais que isto é descartada da memória.
VALIDADE_CACHE = 5 * 60

EXEMPLO = """host = "http://localhost:8080"
apikey = "sua-apikey"
instance = "minha-instancia"
me = "558199990000"
"""


def acervo_tamanho() -> int:
    """Bytes da mídia baixada. NAO e' cache: nunca e' apagada automaticamente.

    E' o unico diretorio que cresce sem teto (o cache abaixo fica na casa dos
    megabytes). Existe para ser mostrado, nao para ser rotacionado — o CDN do
    WhatsApp expira e o que sair daqui nao volta.
    """
    try:
        return sum(f.stat().st_size for f in MEDIA.rglob("*") if f.is_file())
    except Exception:
        return 0


def cache_tamanho() -> int:
    """Bytes do que se recria sozinho: respostas cacheadas e fotos de perfil.

    A midia NAO entra: mora em ~/.local/share e e' acervo (ver acervo_tamanho).
    """
    total = 0
    for base in (CACHE, FOTOS):
        try:
            total += sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
        except Exception:
            pass
    return total


def cache_rotacionar(limite_gb: float) -> int:
    """Apaga o mais antigo até caber no limite. Devolve quantos bytes saíram.

    ponytail: só o cache é rotacionado. A mídia em ~/.local/share é acervo e
    nunca entra aqui — perder aquilo é irreversível, o CDN do WhatsApp expira.
    """
    limite = int(limite_gb * 1024**3)
    if limite <= 0 or cache_tamanho() <= limite:
        return 0
    arquivos = []
    for base in (CACHE, FOTOS):
        try:
            arquivos += [f for f in base.rglob("*") if f.is_file()]
        except Exception:
            pass
    arquivos.sort(key=lambda f: f.stat().st_mtime)  # mais antigo primeiro
    liberado, atual = 0, cache_tamanho()
    for f in arquivos:
        if atual <= limite:
            break
        try:
            n = f.stat().st_size
            f.unlink()
            atual -= n
            liberado += n
        except Exception:
            pass
    return liberado


def cache_ler(nome: str, validade_s: int) -> object | None:
    """Devolve o cache se existir e não estiver velho demais."""
    arq = CACHE / f"{nome}.json"
    try:
        if time.time() - arq.stat().st_mtime > validade_s:
            return None
        return json.loads(arq.read_text())
    except Exception:
        return None


def cache_gravar(nome: str, dado: object) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / f"{nome}.tmp"
        tmp.write_text(json.dumps(dado, ensure_ascii=False))
        tmp.replace(CACHE / f"{nome}.json")  # troca atômica: nunca meio-arquivo
    except Exception:
        pass


def gravar_json(destino: pathlib.Path, dado: object, **kw) -> None:
    """Grava estado local trocando o arquivo inteiro de uma vez.

    Sao preferencias que so' existem aqui (fixados, arquivadas, favoritas...).
    Morrer no meio de um write_text deixava meio JSON, e a leitura cai em
    `except -> vazio`: o estado sumia calado, sem nem um erro na tela.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(json.dumps(dado, **kw))
    tmp.replace(destino)


def salvar_config(cfg: dict) -> None:
    """ponytail: tomllib so le. Escrever TOML de valores simples e' este loop."""
    linhas = []
    for chave, valor in cfg.items():
        if isinstance(valor, bool):
            linhas.append(f"{chave} = {str(valor).lower()}")
        elif isinstance(valor, (int, float)):
            linhas.append(f"{chave} = {valor}")
        else:
            texto = str(valor).replace("\\", "\\\\").replace('"', '\\"')
            linhas.append(f'{chave} = "{texto}"')
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    # Ctrl+= grava isto a cada passo de zoom, do POOL: dois write_text no mesmo
    # caminho se truncam. tmp+replace troca o arquivo inteiro de uma vez — e a
    # apikey mora aqui, meio-arquivo impede o app de abrir.
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text("\n".join(linhas) + "\n")
    tmp.chmod(0o600)
    tmp.replace(CONFIG)


def load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(EXEMPLO)
        raise SystemExit(f"Criei {CONFIG} — preencha a apikey e rode de novo.")
    try:
        cfg = tomllib.loads(CONFIG.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"{CONFIG} está corrompido ({e}). Apague-o e rode de novo.") from e
    faltando = {"host", "apikey", "instance"} - set(cfg)
    if faltando:
        raise SystemExit(f"{CONFIG}: faltam as chaves {sorted(faltando)}")
    return cfg


APAGADA = "Mensagem apagada"


_URL = re.compile(r"(https?://[^\s<>\"]+|www\.[^\s<>\"]+)")
_MONO_BLOCO = re.compile(r"```(.+?)```", re.S)
_MONO_INLINE = re.compile(r"`([^`\n]+)`")
# nas bordas, so' vale se o delimitador nao estiver colado em letra/numero —
# senao "a_b_c" e datas viram italico
_NEGRITO = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALICO = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_RISCADO = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")


def _escapar(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def formatar(texto: str, cor_link: str = "#53bdeb") -> str:
    """A marcação do WhatsApp em HTML: *negrito* _itálico_ ~riscado~,
    ```bloco``` e `código`, listas com - ou 1., e > citação.

    O texto chega cru do servidor — os asteriscos apareciam na tela porque o
    balão desenhava com drawText, que não interpreta marcação nenhuma.
    """
    guardados: list[str] = []

    def guardar(html: str) -> str:
        guardados.append(html)
        return f"\x00{len(guardados) - 1}\x00"

    # o monoespaçado é literal: sai de cena antes de qualquer outra regra
    texto = _MONO_BLOCO.sub(
        lambda m: guardar(f"<pre style='margin:2px 0'>{_escapar(m.group(1))}</pre>"), texto
    )
    texto = _MONO_INLINE.sub(
        lambda m: guardar(f"<code>{_escapar(m.group(1))}</code>"), texto
    )
    texto = _escapar(texto)
    texto = _URL.sub(
        lambda m: guardar(
            f"<a href='{m.group(0)}' style='color:{cor_link}'>{m.group(0)}</a>"
        ),
        texto,
    )

    linhas, saida = texto.split("\n"), []
    for linha in linhas:
        nua = linha.lstrip()
        if nua.startswith("&gt; "):
            saida.append(
                "<div style='border-left:3px solid #667781; padding-left:8px;"
                f" color:#c9d1d4'>{nua[5:]}</div>"
            )
        elif re.match(r"^[-*]\s+\S", nua):
            saida.append(f"&nbsp;&nbsp;•&nbsp;{nua[2:]}")
        elif m := re.match(r"^(\d+)\.\s+(\S.*)$", nua):
            saida.append(f"&nbsp;&nbsp;{m.group(1)}.&nbsp;{m.group(2)}")
        else:
            saida.append(linha)
    texto = "<br>".join(saida)

    texto = _NEGRITO.sub(r"<b>\1</b>", texto)
    texto = _ITALICO.sub(r"<i>\1</i>", texto)
    texto = _RISCADO.sub(r"<s>\1</s>", texto)

    for i, html in enumerate(guardados):
        texto = texto.replace(f"\x00{i}\x00", html)
    return texto


def foi_apagada(msg: dict) -> bool:
    """Apagada para todos. O texto continua no banco da Evolution, mas
    `key.deleted` fica true — é ele que manda, nunca o conteúdo."""
    return bool((msg.get("key") or {}).get("deleted")) or msg.get("status") == "DELETED"


def text_of(msg: dict) -> str:
    """Texto exibível de uma mensagem, seja qual for o tipo."""
    if foi_apagada(msg):
        return APAGADA
    m = msg.get("message") or {}
    for caminho in ("conversation",):
        if m.get(caminho):
            return m[caminho]
    for chave in ("extendedTextMessage", "imageMessage", "videoMessage"):
        sub = m.get(chave) or {}
        if sub.get("text") or sub.get("caption"):
            return sub.get("text") or sub["caption"]
    # localização tem nome/endereço no payload (medido: "Recife", "PE") — jogar
    # fora e mostrar só o genérico escondia o que a pessoa mandou
    if "locationMessage" in m:
        loc = m.get("locationMessage") or {}
        lugar = " — ".join(x for x in (loc.get("name"), loc.get("address")) if x)
        return f"📍 {lugar}" if lugar else "📍 Localização"
    if "contactMessage" in m:
        cont = m.get("contactMessage") or {}
        quem = (cont.get("displayName") or "").strip()
        return f"👤 {quem}" if quem else "👤 Contato"
    if "pollCreationMessage" in m:
        # a bolha mostra a pergunta + opções; votar exige o messageSecret e o
        # endpoint de voto — fica para quando houver uma enquete real no acervo
        poll = m.get("pollCreationMessage") or {}
        opcoes = [f"• {(o.get('optionName') or '').strip()}" for o in
                  (poll.get("options") or []) if (o.get("optionName") or "").strip()]
        pergunta = (poll.get("name") or "Enquete").strip()
        corpo = "📊 " + pergunta
        if opcoes:
            corpo += "\n" + "\n".join(opcoes)
        return corpo
    rotulos = {
        "audioMessage": "🎤 Áudio",
        "stickerMessage": "🏷 Figurinha",
        "documentMessage": "📄 Documento",
        "imageMessage": "🖼 Imagem",
        "videoMessage": "🎬 Vídeo",
    }
    doc = m.get("documentMessage") or {}
    if doc.get("fileName"):
        return f"📄 {doc['fileName']}"
    return rotulos.get(msg.get("messageType", ""), "")


# Códigos de país mais usados aqui. ponytail: a lista completa da ITU tem 240
# entradas e ninguém rola até "Wallis e Futuna" — o campo aceita digitar o
# código à mão para qualquer país fora daqui.
PAISES = [
    ("BR", "Brasil", "55"), ("PT", "Portugal", "351"), ("US", "Estados Unidos/Canadá", "1"),
    ("AR", "Argentina", "54"), ("UY", "Uruguai", "598"), ("PY", "Paraguai", "595"),
    ("CL", "Chile", "56"), ("CO", "Colômbia", "57"), ("MX", "México", "52"),
    ("PE", "Peru", "51"), ("BO", "Bolívia", "591"), ("ES", "Espanha", "34"),
    ("IT", "Itália", "39"), ("FR", "França", "33"), ("DE", "Alemanha", "49"),
    ("GB", "Reino Unido", "44"), ("IE", "Irlanda", "353"), ("NL", "Holanda", "31"),
    ("CH", "Suíça", "41"), ("AO", "Angola", "244"), ("MZ", "Moçambique", "258"),
    ("CV", "Cabo Verde", "238"), ("JP", "Japão", "81"), ("CN", "China", "86"),
    ("IN", "Índia", "91"), ("AU", "Austrália", "61"),
]


def bandeira(iso: str) -> str:
    """🇧🇷 a partir de "BR" — dois Regional Indicator Symbols, sem tabela."""
    if len(iso) != 2 or not iso.isalpha():
        return "🏳"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


def montar_numero(ddi: str, nacional: str) -> str:
    """Junta código do país e número nacional, tirando o lixo de digitação."""
    ddi = "".join(c for c in ddi if c.isdigit())
    nacional = "".join(c for c in nacional if c.isdigit())
    if not ddi or not nacional:
        return ""
    if nacional.startswith(ddi) and len(nacional) > len(ddi) + 7:
        return nacional  # já veio com o código do país colado
    return ddi + nacional


def formatar_numero(jid: str) -> str:
    """558133334444@s.whatsapp.net -> +55 81 3333-4444.

    LID não é telefone: tem 14+ dígitos e não corresponde a nenhum país.
    Formatá-lo como número produz coisas como "+123456789012345", que engana.
    """
    n = jid.split("@")[0].split(":")[0]
    if not n.isdigit():
        return n
    if jid.endswith("@lid") or len(n) > 13:
        return "Contato"
    if n.startswith("55") and len(n) in (12, 13):
        ddd, resto = n[2:4], n[4:]
        return f"+55 {ddd} {resto[:-4]}-{resto[-4:]}"
    return f"+{n}"


def nome_do_chat(chat: dict, contatos: dict[str, str] | None = None) -> str:
    """Nome exibível. O WhatsApp migrou para @lid, que não carrega pushName —
    daí a cadeia de fallbacks, cada degrau melhor que mostrar o JID cru.
    """
    contatos = contatos or {}
    jid = chat.get("remoteJid", "")

    apelidos = chat.get("_apelidos") or {}
    for candidato in [jid, *chat.get("_jids", []), identidade(chat)]:
        if nome := (apelidos.get(candidato) or "").strip():
            return nome

    if nome := (chat.get("pushName") or "").strip():
        return nome

    ultima = chat.get("lastMessage") or {}
    chave = ultima.get("key") or {}
    # pushName de mensagem minha e' "Você" — nao diz nada sobre o outro lado
    if not chave.get("fromMe"):
        if nome := (ultima.get("pushName") or "").strip():
            return nome

    alt = chave.get("remoteJidAlt")
    if alt:
        if nome := (contatos.get(alt) or "").strip():
            return nome
        return formatar_numero(alt)

    if nome := (contatos.get(jid) or "").strip():
        return nome
    if jid.endswith("@g.us"):
        return "Grupo"
    if jid.endswith("@lid"):
        return jid.split("@")[0]
    return formatar_numero(jid)


def identidade(chat: dict) -> str:
    """Quem é o contato por trás do chat — o mesmo para @lid e @s.whatsapp.net."""
    jid = chat.get("remoteJid", "")
    if jid.endswith("@g.us"):
        return jid
    alt = ((chat.get("lastMessage") or {}).get("key") or {}).get("remoteJidAlt")
    return (alt or jid).split("@")[0]


def unir_duplicados(chats: list[dict], contatos: dict[str, str] | None = None) -> list[dict]:
    """O WhatsApp migrou para @lid e a Evolution guarda os dois JIDs do mesmo
    contato como chats separados. Une: fica o mais recente, com o melhor nome
    dos dois e a lista de JIDs em `_jids` para o histórico não sumir.
    """
    grupos: dict[str, list[dict]] = {}
    for c in chats:
        grupos.setdefault(identidade(c), []).append(c)

    unidos = []
    for membros in grupos.values():
        membros.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
        principal = dict(membros[0])
        principal["_jids"] = [m["remoteJid"] for m in membros]
        if len(membros) > 1:
            # o nome bom pode estar no chat menos ativo
            for m in membros:
                nome = nome_do_chat(m, contatos)
                if nome and not nome.startswith("+") and not nome.isdigit():
                    principal["pushName"] = nome
                    break
            for m in membros:
                if m.get("profilePicUrl"):
                    principal["profilePicUrl"] = m["profilePicUrl"]
                    break
        unidos.append(principal)

    unidos.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
    return unidos


OCULTAS = {"reactionMessage", "protocolMessage", "senderKeyDistributionMessage",
           "messageContextInfo", "pollUpdateMessage", "associatedChildMessage",
           # sem conteúdo renderizável no nosso lado — bolha vazia não pode:
           # albumMessage é só o envelope do álbum (as mídias vêm separadas),
           # templateMessage sem corpo é consentimento de URL (medido: Rodobens),
           # secretEncryptedMessage não sai da criptografia de segredo
           "albumMessage", "templateMessage", "secretEncryptedMessage", "unknown"}


def e_visivel(msg: dict) -> bool:
    """Reação e mensagem de protocolo não são bolhas — não podem virar linha."""
    return msg.get("messageType") not in OCULTAS


def reacao_de(msg: dict) -> tuple[str, str, str] | None:
    """(id da mensagem alvo, emoji, autor). emoji vazio = reação removida.

    Quem reagiu está em `key.participant` do envelope — **não** no
    `reactionMessage.key.participant`, que é o autor da mensagem *alvo*.
    Em conversa 1:1 o participant vem vazio e o autor é o próprio remoteJid;
    a minha reação é sempre `fromMe`, então nunca colide com a do contato.
    """
    r = (msg.get("message") or {}).get("reactionMessage")
    if not r:
        return None
    alvo = (r.get("key") or {}).get("id")
    if not alvo:
        return None
    chave = msg.get("key") or {}
    if chave.get("fromMe"):
        autor = "eu"
    else:
        autor = chave.get("participant") or chave.get("remoteJid") or "?"
    return (alvo, r.get("text") or "", autor)


def aplicar_reacao(alvo: dict, autor: str, emoji: str) -> None:
    """Guarda a reação POR PESSOA: uma por autor, a última vence.

    Guardar só o emoji funde duas pessoas que reagiram igual e mostra três
    bolhas quando alguém trocou de emoji três vezes. O WhatsApp conta gente.
    """
    reacoes = alvo.get("_reacoes")
    if not isinstance(reacoes, dict):
        reacoes = {}
    if emoji:
        reacoes[autor] = emoji
    else:
        reacoes.pop(autor, None)
    if reacoes:
        alvo["_reacoes"] = reacoes
    else:
        alvo.pop("_reacoes", None)


def resumo_reacoes(msg: dict) -> tuple[str, int]:
    """(emojis distintos na ordem de chegada, quantas pessoas reagiram)."""
    reacoes = msg.get("_reacoes")
    if not isinstance(reacoes, dict) or not reacoes:
        return ("", 0)
    return ("".join(dict.fromkeys(reacoes.values())), len(reacoes))


def aplicar_reacoes(registros: list[dict]) -> list[dict]:
    """Tira as reações da lista e pendura cada uma na mensagem que ela reage."""
    porc_alvo: dict[str, dict[str, str]] = {}
    # os registros chegam do mais novo para o mais velho; reagir e depois
    # desfazer so' da' o resultado certo se lido em ordem cronologica
    for m in reversed(registros):
        if par := reacao_de(m):
            alvo, emoji, autor = par
            atual = porc_alvo.setdefault(alvo, {})
            if emoji:
                atual[autor] = emoji
            else:
                atual.pop(autor, None)
    visiveis = [m for m in registros if e_visivel(m)]
    for m in visiveis:
        reacoes = porc_alvo.get((m.get("key") or {}).get("id"))
        if reacoes:
            m["_reacoes"] = dict(reacoes)
        else:
            m.pop("_reacoes", None)  # limpa sobra de uma passada anterior
    return visiveis


ICONES = {
    "imageMessage": "🖼", "videoMessage": "🎬", "audioMessage": "🎤",
    "documentMessage": "📄", "stickerMessage": "🏷", "locationMessage": "📍",
    "contactMessage": "👤", "contactsArrayMessage": "👤", "pollCreationMessage": "📊",
}


def previa(chat: dict, contatos: dict[str, str] | None = None) -> tuple[str, str, str]:
    """(marca de leitura, autor, texto) da última mensagem — o formato da
    barra lateral do WhatsApp Web: "✓✓ Você: 🖼 legenda"."""
    msg = chat.get("lastMessage") or {}
    if not msg:
        return "", "", ""
    chave = msg.get("key") or {}
    meu = bool(chave.get("fromMe"))
    marca = marca_de(msg)[0] if meu else ""

    autor = ""
    if meu:
        autor = "Você"
    elif (chat.get("remoteJid") or "").endswith("@g.us"):
        autor = (msg.get("pushName") or "").strip()

    tipo = msg.get("messageType", "")
    icone = ICONES.get(tipo, "")
    corpo = text_of(msg)
    if tipo == "audioMessage":
        seg = _num(((msg.get("message") or {}).get("audioMessage") or {}).get("seconds"))
        corpo = f"{seg // 60}:{seg % 60:02d}" if seg else ""
    elif icone and corpo.startswith(icone):
        corpo = corpo[len(icone):].strip()
    texto = f"{icone} {corpo}".strip() if icone else corpo
    return marca, autor, texto


# Escala de confirmação do WhatsApp (WAMessageStatus do Baileys), do menos
# ao mais avançado. Os nomes batem com o que a Evolution grava no banco.
PROGRESSO = ["ERROR", "PENDING", "SERVER_ACK", "DELIVERY_ACK", "READ", "PLAYED"]

# 🕐 nao enviado · ✓ no servidor · ✓✓ no aparelho · ✓✓ azul lido/ouvido
MARCAS = {
    "ERROR": ("⚠", False),
    "PENDING": ("🕐", False),
    "SERVER_ACK": ("✓", False),
    "DELIVERY_ACK": ("✓✓", False),
    "READ": ("✓✓", True),
    "PLAYED": ("✓✓", True),
}


def status_de(msg: dict) -> str:
    """Status mais avançado da mensagem.

    `findMessages` devolve `status` vazio e uma lista `MessageUpdate` fora de
    ordem — ex.: [SERVER_ACK, DELIVERY_ACK, SERVER_ACK]. Vale o mais avançado,
    nunca o último.
    """
    candidatos = []
    if s := msg.get("status"):
        candidatos.append(s)
    for u in msg.get("MessageUpdate") or []:
        if isinstance(u, dict) and u.get("status"):
            candidatos.append(u["status"])
    validos = [c for c in candidatos if c in PROGRESSO]
    if not validos:
        # esta no historico do servidor: no minimo saiu daqui
        return "EDITED" if "EDITED" in candidatos else "SERVER_ACK"
    return max(validos, key=PROGRESSO.index)


def marca_de(msg: dict) -> tuple[str, bool]:
    """(símbolo, se é azul) para o rodapé da bolha."""
    if not (msg.get("key") or {}).get("fromMe"):
        return "", False
    return MARCAS.get(status_de(msg), ("✓", False))


def foi_editada(msg: dict) -> bool:
    if msg.get("_editada") or msg.get("status") == "EDITED":
        return True
    return any(
        isinstance(u, dict) and u.get("status") == "EDITED"
        for u in msg.get("MessageUpdate") or []
    )


def _num(v) -> int:
    """O Baileys serializa inteiros longos como {low, high, unsigned}."""
    if isinstance(v, dict):
        return int(v.get("low") or 0) + (int(v.get("high") or 0) << 32)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def detalhes(msg: dict) -> dict:
    """Campos que o balão precisa para desenhar cartões ricos como o WhatsApp Web:
    duração do áudio, tamanho e nome do documento, coordenadas, contato, link."""
    m = msg.get("message") or {}
    tipo = msg.get("messageType", "")
    d: dict = {"tipo": tipo}

    if a_ := m.get("audioMessage"):
        d["segundos"] = _num(a_.get("seconds"))
        d["ptt"] = bool(a_.get("ptt"))
        # O WhatsApp MANDA a onda pronta: 64 amostras de 0 a 100, no payload
        # (99% dos 18.808 áudios do acervo têm). É a mesma que o Web desenha.
        # Sem isto a bolha ficava sem onda até baixar o arquivo e rodar ffmpeg.
        d["onda"] = a_.get("waveform") or None
    if doc := m.get("documentMessage"):
        d["nome"] = doc.get("fileName") or "documento"
        d["bytes"] = _num(doc.get("fileLength"))
        d["mime"] = doc.get("mimetype") or ""
        d["paginas"] = doc.get("pageCount")
    if loc := m.get("locationMessage"):
        d["lat"] = loc.get("degreesLatitude")
        d["lon"] = loc.get("degreesLongitude")
        d["nome"] = loc.get("name") or ""
        d["endereco"] = loc.get("address") or ""
    if con := (m.get("contactMessage") or (m.get("contactsArrayMessage") or {})):
        if con.get("displayName"):
            d["contato"] = con["displayName"]
            d["vcard"] = con.get("vcard", "")
    ext = m.get("extendedTextMessage") or {}
    if ext.get("title") or ext.get("canonicalUrl") or ext.get("matchedText"):
        d["link"] = {
            "titulo": ext.get("title") or "",
            "descricao": ext.get("description") or "",
            "url": ext.get("canonicalUrl") or ext.get("matchedText") or "",
            # 35/43 previews de link trazem a thumb EMBUTIDA no payload (média
            # 6,3KB, ~140px de largura real) — o cartão ganha imagem sem rede.
            "thumb": ext.get("jpegThumbnail") or "",
        }
    if v := m.get("videoMessage"):
        d["segundos"] = _num(v.get("seconds"))
        d["largura"], d["altura"] = _num(v.get("width")), _num(v.get("height"))
        # GIF no WhatsApp é MP4 sem áudio com gifPlayback:true (1.067 no acervo)
        d["gif"] = bool(v.get("gifPlayback"))
    if img := m.get("imageMessage"):
        d["largura"], d["altura"] = _num(img.get("width")), _num(img.get("height"))
        # remetente com app oficial em HD manda full + versão média separada;
        # a presença de midQualityFileSha256 é o marcador de HD no payload
        d["hd"] = bool(img.get("midQualityFileSha256"))

    # o Baileys embute uma miniatura JPEG de 1-4 KB no próprio payload: 85% dos
    # vídeos e 80% das imagens têm. Serve para a bolha aparecer completa antes
    # de qualquer download.
    # cuidado: message["conversation"] é uma string, não um dicionário
    sub = m.get(tipo)
    if isinstance(sub, dict) and (thumb := sub.get("jpegThumbnail")):
        d["thumb"] = thumb
    return d


def tamanho_humano(n: int) -> str:
    if n <= 0:
        return ""
    for unidade, divisor in (("MB", 1_048_576), ("kB", 1024)):
        if n >= divisor:
            return f"{n / divisor:.0f} {unidade}"
    return f"{n} B"


def media_kind(message_type: str) -> str | None:
    """Categoria de mídia, ou None se a mensagem não tem anexo."""
    return {
        "imageMessage": "image",
        "videoMessage": "video",
        "audioMessage": "audio",
        "documentMessage": "document",
        "stickerMessage": "image",
    }.get(message_type)


class Api:
    def __init__(self, cfg: dict):
        self.instance = cfg["instance"]
        self.me = cfg.get("me", "")
        self.host = cfg["host"].rstrip("/")
        self.apikey = cfg["apikey"]
        self.http = httpx.Client(
            base_url=self.host, headers={"apikey": self.apikey}, timeout=60
        )
        MEDIA.mkdir(parents=True, exist_ok=True)
        FOTOS.mkdir(parents=True, exist_ok=True)
        self._grupos: list[dict] | None = None
        self.pg = (
            {
                "host": cfg["pg_host"],
                "port": int(cfg.get("pg_port") or 5432),
                "user": cfg.get("pg_user") or "evolution",
                "password": cfg.get("pg_password") or "",
                "dbname": cfg.get("pg_database") or "evolution",
            }
            if cfg.get("pg_host")
            else None
        )

    # ---------- leitura ----------

    def chats(self, gravar: bool = True) -> list[dict]:
        """Sidebar pronta: já vem ordenada por atividade, com lastMessage embutido."""
        r = self.http.post(f"/chat/findChats/{self.instance}", json={})
        r.raise_for_status()
        dados = r.json()
        if gravar and dados:
            cache_gravar("chats", dados)
        return dados

    def chats_do_cache(self) -> list[dict] | None:
        """O que dá para pintar antes de a rede responder. 7 dias: mesmo velho,
        é melhor que tela vazia — o socket corrige em segundos."""
        dado = cache_ler("chats", 7 * 86400)
        return dado if isinstance(dado, list) and dado else None

    def contatos_do_cache(self) -> dict[str, str] | None:
        dado = cache_ler("contatos", 7 * 86400)
        return dado if isinstance(dado, dict) and dado else None

    def contatos(self) -> dict[str, str]:
        """remoteJid -> nome, para resolver os chats que vêm sem pushName."""
        try:
            r = self.http.post(f"/chat/findContacts/{self.instance}", json={})
            mapa = {
                c["remoteJid"]: c["pushName"]
                for c in r.json()
                if c.get("remoteJid") and (c.get("pushName") or "").strip()
            }
            if mapa:
                cache_gravar("contatos", mapa)
            return mapa
        except Exception:
            return {}

    def messages_recentes(
        self, jids: list[str], janela_s: int = JANELA, minimo: int = 40,
        teto: int = TETO_JANELA, progresso=None,
    ) -> tuple[list[dict], int, bool]:
        """As mensagens da janela recente (padrão: 72h).

        A API **ignora** filtro de data — `messageTimestamp.gte` devolve a
        conversa inteira e, com `lte` junto, devolve zero. Mas a ordem já vem
        DESC, então basta pedir páginas até cruzar o limite.

        Devolve (mensagens, páginas totais, tem_mais_antigo).
        """
        corte = int(time.time()) - janela_s
        acumulado: list[dict] = []
        pagina, paginas = 1, 1
        while True:
            if progresso:
                # progresso REAL: quantas mensagens ja' chegaram de fato, e a que
                # distancia a mais antiga esta' da borda da janela de 72h
                progresso(len(acumulado), teto, f"página {pagina}")
            registros, paginas = self.messages(jids, pagina, 50)
            if not registros:
                return (aplicar_reacoes(acumulado), paginas, False)
            acumulado += registros
            mais_velha = min(m.get("messageTimestamp", 0) for m in registros)
            passou = mais_velha < corte
            # a janela manda, mas garante um mínimo (conversa parada) e um teto
            # (grupo com milhares de mensagens em 72h)
            if progresso:
                idade = max(0, int(time.time()) - mais_velha)
                progresso(len(acumulado), teto,
                          f"{len(acumulado)} mensagens · {idade // 3600}h de histórico")
            if (passou and len(acumulado) >= minimo) or len(acumulado) >= teto:
                break
            if pagina >= paginas:
                return (aplicar_reacoes(acumulado), paginas, False)
            pagina += 1
        return (aplicar_reacoes(acumulado), paginas, True)

    def messages(
        self, jid: str | list[str], page: int = 1, size: int = 50
    ) -> tuple[list[dict], int]:
        """Página de mensagens, mais nova primeiro. `offset` é o TAMANHO da página.

        Aceita vários JIDs (contato que existe em @lid e em @s.whatsapp.net) e
        mescla por timestamp — senão metade do histórico fica invisível.
        """
        jids = [jid] if isinstance(jid, str) else list(jid)
        registros: list[dict] = []
        paginas = 1
        for j in jids:
            r = self.http.post(
                f"/chat/findMessages/{self.instance}",
                json={"where": {"key": {"remoteJid": j}}, "page": page, "offset": size},
            )
            r.raise_for_status()
            bloco = r.json()["messages"]
            registros += bloco["records"]
            paginas = max(paginas, bloco["pages"])
        if len(jids) > 1:
            registros.sort(key=lambda m: m.get("messageTimestamp", 0), reverse=True)
            registros = registros[:size]
        return aplicar_reacoes(registros), paginas

    # ---------- envio ----------

    def send_text(self, number: str, text: str, quoted: dict | None = None) -> dict:
        corpo = {"number": number, "text": text}
        if quoted:
            corpo["quoted"] = {"key": quoted}
        r = self.http.post(f"/message/sendText/{self.instance}", json=corpo)
        r.raise_for_status()
        return r.json()

    def send_audio(self, number: str, path: pathlib.Path) -> dict:
        """PTT. A Evolution converte para ogg/opus sozinha — verificado com WAV."""
        r = self.http.post(
            f"/message/sendWhatsAppAudio/{self.instance}",
            json={"number": number, "audio": base64.b64encode(path.read_bytes()).decode()},
        )
        r.raise_for_status()
        return r.json()

    def send_video_nota(self, number: str, path: pathlib.Path) -> dict:
        """Vídeo-recado redondo."""
        r = self.http.post(
            f"/message/sendPtv/{self.instance}",
            json={"number": number, "video": base64.b64encode(path.read_bytes()).decode()},
        )
        r.raise_for_status()
        return r.json()

    def editar(self, number: str, key: dict, texto: str) -> dict:
        r = self.http.post(
            f"/chat/updateMessage/{self.instance}",
            json={"number": number, "key": key, "text": texto},
        )
        r.raise_for_status()
        return r.json()

    def apagar(self, key: dict) -> dict:
        """Apagar para todos. 'Apagar para mim' não existe na API — é local."""
        r = self.http.request(
            "DELETE", f"/chat/deleteMessageForEveryone/{self.instance}", json=key
        )
        r.raise_for_status()
        return r.json()

    def marcar_lida(self, keys: list[dict]) -> None:
        if keys:
            self.http.post(
                f"/chat/markMessageAsRead/{self.instance}", json={"readMessages": keys}
            )

    def presenca(self, number: str, estado: str = "composing", ms: int = 2000) -> None:
        """composing | recording | paused — o 'digitando…' do outro lado."""
        try:
            self.http.post(
                f"/chat/sendPresence/{self.instance}",
                json={"number": number, "presence": estado, "delay": ms},
            )
        except Exception:
            pass  # presenca e' enfeite: nunca pode derrubar o envio

    def send_sticker(self, number: str, path: pathlib.Path) -> dict:
        r = self.http.post(
            f"/message/sendSticker/{self.instance}",
            json={"number": number, "sticker": base64.b64encode(path.read_bytes()).decode()},
        )
        r.raise_for_status()
        return r.json()

    def send_location(self, number: str, lat: float, lon: float, nome: str = "", end: str = "") -> dict:
        r = self.http.post(
            f"/message/sendLocation/{self.instance}",
            json={"number": number, "latitude": lat, "longitude": lon,
                  "name": nome or "Localização", "address": end},
        )
        r.raise_for_status()
        return r.json()

    def send_contact(self, number: str, nome: str, telefone: str) -> dict:
        r = self.http.post(
            f"/message/sendContact/{self.instance}",
            json={"number": number, "contact": [
                {"fullName": nome, "wuid": telefone, "phoneNumber": telefone}
            ]},
        )
        r.raise_for_status()
        return r.json()

    def perfil(self, jid: str) -> dict:
        """Nome, foto e recado ("status") do contato."""
        try:
            r = self.http.post(f"/chat/fetchProfile/{self.instance}", json={"number": jid})
            if not r.is_success:
                return {}
            d = r.json()
            recado = d.get("status")
            if isinstance(recado, dict):
                recado = recado.get("status")
            return {
                "nome": d.get("name") or "",
                "foto": d.get("picture") or "",
                "recado": (recado or "").strip(),
                "existe": bool(d.get("numberExists", True)),
            }
        except Exception:
            return {}

    def todos_os_grupos(self, com_participantes: bool = True) -> list[dict]:
        # fetchAllGroups com participantes leva ~41s. Memória para a sessão,
        # disco para as próximas — participante de grupo não muda por hora.
        if self._grupos is not None:
            return self._grupos
        if (guardado := cache_ler("grupos", 24 * 3600)) is not None:
            self._grupos = guardado
            return self._grupos
        try:
            r = self.http.get(
                f"/group/fetchAllGroups/{self.instance}",
                params={"getParticipants": "true" if com_participantes else "false"},
                timeout=90,
            )
            self._grupos = r.json() if r.is_success else []
            if self._grupos:
                cache_gravar("grupos", self._grupos)
            return self._grupos
        except Exception:
            return []

    def grupos_em_comum(self, jids: list[str], grupos: list[dict] | None = None) -> list[dict]:
        """Grupos onde o contato está. Os participantes vêm com `id` em @lid e
        `phoneNumber` em @s.whatsapp.net — é preciso casar pelos dois."""
        alvos = {j.split("@")[0] for j in jids if j}
        if not alvos:
            return []
        achados = []
        for g in grupos if grupos is not None else self.todos_os_grupos():
            for p in g.get("participants") or []:
                ids = {
                    str(p.get("id", "")).split("@")[0],
                    str(p.get("phoneNumber", "")).split("@")[0],
                }
                if ids & alvos:
                    achados.append(g)
                    break
        return achados

    def existe_no_whatsapp(self, numeros: list[str]) -> dict[str, dict]:
        """{numero: {jid, exists, name}} — checa antes de tentar conversar."""
        try:
            r = self.http.post(
                f"/chat/whatsappNumbers/{self.instance}", json={"numbers": numeros}
            )
            if not r.is_success:
                return {}
            return {x["number"]: x for x in r.json()}
        except Exception:
            return {}

    def sou_admin(self, info: dict) -> bool:
        """Se o meu número consta como admin na lista de participantes."""
        meu = "".join(c for c in (self.me or "") if c.isdigit())
        if not meu:
            return False
        for x in info.get("participants") or []:
            ids = (str(x.get("id", "")) + str(x.get("phoneNumber", "")))
            if meu and meu in ids:
                return bool(x.get("admin"))
        return False

    def resumo_participantes(self, info: dict, contatos: dict[str, str] | None = None) -> str:
        """"Ana, Bruno, Você" — o subtítulo do cabeçalho no WhatsApp Web."""
        contatos = contatos or {}
        meu = "".join(c for c in (self.me or "") if c.isdigit())
        nomes = []
        for x in info.get("participants") or []:
            ids = str(x.get("id", "")) + str(x.get("phoneNumber", ""))
            if meu and meu in ids:
                nomes.append("Você")
                continue
            achado = (
                contatos.get(str(x.get("id", "")))
                or contatos.get(str(x.get("phoneNumber", "")))
                or ""
            ).strip()
            nomes.append(achado or formatar_numero(str(x.get("phoneNumber") or x.get("id", ""))))
        vistos, limpos = set(), []
        for n in nomes:
            if n not in vistos:
                vistos.add(n)
                limpos.append(n)
        return ", ".join(limpos)

    def grupo_info(self, jid: str) -> dict:
        r = self.http.get(f"/group/findGroupInfos/{self.instance}", params={"groupJid": jid})
        return r.json() if r.is_success else {}

    # ---------- meu perfil e privacidade ----------
    #
    # Rotas conferidas na fonte do tag 2.3.7 (chat.router.ts) e os valores de
    # privacidade nos tipos do Baileys — nao adivinhados. Medido contra a
    # instancia: fetchPrivacySettings devolve as 6 chaves abaixo.

    # o que cada chave aceita; a UI monta os menus a partir daqui
    PRIVACIDADE = {
        "readreceipts": ("Confirmações de leitura", ["all", "none"]),
        "profile": ("Foto do perfil", ["all", "contacts", "contact_blacklist", "none"]),
        "status": ("Recado", ["all", "contacts", "contact_blacklist", "none"]),
        "last": ("Visto por último", ["all", "contacts", "contact_blacklist", "none"]),
        "online": ("Online", ["all", "match_last_seen"]),
        "groupadd": ("Quem pode me adicionar a grupos",
                     ["all", "contacts", "contact_blacklist"]),
    }

    def privacidade(self) -> dict:
        r = self.http.get(f"/chat/fetchPrivacySettings/{self.instance}")
        return r.json() if r.is_success else {}

    def privacidade_definir(self, ajustes: dict) -> None:
        """Manda SEMPRE as 6 chaves. Lido na fonte do tag 2.3.7:

            await this.client.updateReadReceiptsPrivacy(settings.readreceipts);
            await this.client.updateProfilePicturePrivacy(settings.profile);
            ... as seis, incondicionalmente

        Chave ausente no corpo vira `undefined` na chamada do Baileys — nao e'
        "nao mexe nessa", e' mexer com lixo. Por isso exigimos as 6.

        Um valor que a lista do Baileys nao previa (WhatsApp adicionou algo)
        passa adiante: foi o proprio servidor que o devolveu, e devolve-lo
        intacto e' mais seguro do que dropar a chave.

        Efeito colateral medido na fonte: a rota chama `reloadConnection()` —
        gravar privacidade RELIGA a sessao. Quem chama tem de avisar.
        """
        faltando = set(self.PRIVACIDADE) - set(ajustes)
        if faltando:
            raise ValueError(
                f"privacidade exige as 6 chaves; faltam {sorted(faltando)}. "
                "Chave ausente vira undefined no Baileys."
            )
        r = self.http.post(f"/chat/updatePrivacySettings/{self.instance}", json=ajustes)
        r.raise_for_status()

    def meu_perfil(self) -> dict:
        """nome, recado e foto do proprio dono."""
        r = self.http.post(f"/chat/fetchProfile/{self.instance}",
                           json={"number": self.me or ""})
        if not r.is_success:
            return {}
        d = r.json() or {}
        return {"nome": d.get("name") or "", "foto": d.get("picture") or "",
                "recado": ((d.get("status") or {}).get("status") or "").strip()}

    def perfil_nome(self, nome: str) -> None:
        r = self.http.post(f"/chat/updateProfileName/{self.instance}", json={"name": nome})
        r.raise_for_status()

    def perfil_recado(self, recado: str) -> None:
        r = self.http.post(f"/chat/updateProfileStatus/{self.instance}",
                           json={"status": recado})
        r.raise_for_status()

    def perfil_foto(self, caminho: pathlib.Path) -> None:
        """A rota aceita url OU base64 no campo `picture`."""
        dados = base64.b64encode(caminho.read_bytes()).decode()
        r = self.http.post(f"/chat/updateProfilePicture/{self.instance}",
                           json={"number": self.me or "", "picture": dados})
        r.raise_for_status()

    def perfil_remover_foto(self) -> None:
        r = self.http.delete(f"/chat/removeProfilePicture/{self.instance}")
        r.raise_for_status()

    # ---------- grupos: gestão ----------
    #
    # Sondado contra a instância em 09/09/2026, escrevendo num grupo meu de um
    # membro só e revertendo. Achados que o payload não anuncia:
    #   * todas respondem **201**, nunca 200 — testar is_success
    #   * `updateSetting` devolve `{}` mesmo dando certo; só o status vale
    #   * `updateGroupDescription` **recusa string vazia** com 400: não há como
    #     apagar a descrição, só trocá-la (usamos um espaço para "limpar")
    #   * `groupJid` vai na QUERY, não no corpo — menos em `create` e `sendInvite`

    def grupo_participantes(self, jid: str) -> list[dict]:
        r = self.http.get(f"/group/participants/{self.instance}", params={"groupJid": jid})
        return (r.json() or {}).get("participants", []) if r.is_success else []

    def grupo_convite(self, jid: str) -> str:
        """Link de convite. Só admin: para o resto a API devolve not-authorized."""
        r = self.http.get(f"/group/inviteCode/{self.instance}", params={"groupJid": jid})
        return (r.json() or {}).get("inviteUrl", "") if r.is_success else ""

    def grupo_revogar_convite(self, jid: str) -> str:
        """Devolve o link NOVO. A resposta traz `inviteCode` cru, sem `inviteUrl`
        — medido: {"revoked":true,"inviteCode":"..."} — então montamos a URL."""
        r = self.http.post(f"/group/revokeInviteCode/{self.instance}",
                           params={"groupJid": jid}, json={})
        r.raise_for_status()
        codigo = (r.json() or {}).get("inviteCode", "") if r.content else ""
        return f"https://chat.whatsapp.com/{codigo}" if codigo else self.grupo_convite(jid)

    def grupo_assunto(self, jid: str, texto: str) -> None:
        r = self.http.post(f"/group/updateGroupSubject/{self.instance}",
                           params={"groupJid": jid}, json={"subject": texto})
        r.raise_for_status()

    def grupo_descricao(self, jid: str, texto: str) -> None:
        # vazio da 400; um espaco e' o mais perto de "sem descricao" que da'
        r = self.http.post(f"/group/updateGroupDescription/{self.instance}",
                           params={"groupJid": jid}, json={"description": texto or " "})
        r.raise_for_status()

    def grupo_participante(self, jid: str, acao: str, numeros: list[str]) -> dict:
        """acao: add · remove · promote · demote. Números só com dígitos."""
        if acao not in ("add", "remove", "promote", "demote"):
            # ValueError, nao assert: `python -O` remove assert e a validacao
            # sumiria — um acao errada chegaria ao POST sem ninguem barrar
            raise ValueError(f"ação de participante inválida: {acao!r}")
        so_digitos = ["".join(c for c in n if c.isdigit()) for n in numeros]
        r = self.http.post(
            f"/group/updateParticipant/{self.instance}", params={"groupJid": jid},
            json={"action": acao, "participants": [n for n in so_digitos if n]},
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    def grupo_ajuste(self, jid: str, acao: str) -> None:
        """announcement = só admin fala · locked = só admin edita os dados."""
        if acao not in ("announcement", "not_announcement", "locked", "unlocked"):
            raise ValueError(f"ajuste de grupo inválido: {acao!r}")
        r = self.http.post(f"/group/updateSetting/{self.instance}",
                           params={"groupJid": jid}, json={"action": acao})
        r.raise_for_status()  # devolve {} mesmo dando certo

    def grupo_temporarias(self, jid: str, segundos: int) -> None:
        """0 desliga. O WhatsApp oferece 24h, 7d e 90d."""
        r = self.http.post(f"/group/toggleEphemeral/{self.instance}",
                           params={"groupJid": jid}, json={"expiration": segundos})
        r.raise_for_status()

    def grupo_sair(self, jid: str) -> None:
        """ponytail: NAO sondado ao vivo — sair de um grupo de verdade nao tem
        desfazer e exigiria convite novo para voltar. A rota e' a documentada
        da 2.3.7; a UI confirma antes de chamar."""
        r = self.http.delete(f"/group/leaveGroup/{self.instance}", params={"groupJid": jid})
        r.raise_for_status()

    def criar_grupo(self, assunto: str, numeros: list[str]) -> dict:
        """ponytail: NAO sondado ao vivo — criaria um grupo de verdade e
        notificaria gente de verdade. A UI confirma antes."""
        so_digitos = ["".join(c for c in n if c.isdigit()) for n in numeros]
        r = self.http.post(
            f"/group/create/{self.instance}",
            json={"subject": assunto, "participants": [n for n in so_digitos if n]},
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    def bloquear(self, jid: str, bloquear: bool = True) -> dict:
        r = self.http.post(
            f"/chat/updateBlockStatus/{self.instance}",
            json={"number": jid, "status": "block" if bloquear else "unblock"},
        )
        r.raise_for_status()
        return r.json()

    # As 3 abas do painel "Mídia, links e docs" do Web. Sticker entra em
    # Mídia (é imagem); áudio NÃO entra em nenhuma — medido no
    # WhatsApp Web: nem a grade nem os Docs do Web mostram nota de voz.
    TIPOS_MIDIA = ("imageMessage", "videoMessage", "stickerMessage")
    TIPOS_DOC = ("documentMessage",)

    def midias_do_chat(
        self, jids: list[str], tipos: tuple[str, ...] | None = None, limite: int = 24
    ) -> tuple[int, list[dict]]:
        """(total, últimas mídias) da conversa, filtrado por tipo.

        Sem `tipos`, junta Mídia+Docs — é o resumo de 4 itens do painel de
        contato. Com `tipos`, alimenta uma aba específica do painel completo.

        Levanta RuntimeError se o Postgres está configurado mas fora de alcance:
        exibir "0" nesse caso seria mentira.
        """
        if not self.pg:
            raise LookupError("Postgres não configurado")
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("psycopg não instalado") from e
        tipos = tipos or (self.TIPOS_MIDIA + self.TIPOS_DOC)
        try:
            with psycopg.connect(**self.pg, connect_timeout=5) as con:
                total = con.execute(
                    """SELECT count(*) FROM "Message"
                       WHERE key->>'remoteJid' = ANY(%s) AND "messageType" = ANY(%s)""",
                    (list(jids), list(tipos)),
                ).fetchone()[0]
                cur = con.execute(
                    """SELECT key, "pushName", "messageType", message, "messageTimestamp"
                       FROM "Message"
                       WHERE key->>'remoteJid' = ANY(%s) AND "messageType" = ANY(%s)
                       ORDER BY "messageTimestamp" DESC LIMIT %s""",
                    (list(jids), list(tipos), limite),
                )
                itens = [
                    {"key": k, "pushName": n, "messageType": mt, "message": m,
                     "messageTimestamp": ts}
                    for k, n, mt, m, ts in cur.fetchall()
                ]
                return (total, itens)
        except Exception as e:
            motivo = str(e).splitlines()[0]
            curto = "senha recusada" if "fe_sendauth" in motivo or "password" in motivo else (
                "sem rota até a porta" if "failed" in motivo else motivo[:44]
            )
            raise RuntimeError(f"Postgres: {curto}") from e

    def links_do_chat(self, jids: list[str], limite: int = 40) -> list[dict]:
        """Mensagens com link — a aba 'Links' do painel de mídia.

        A coluna `"messageType"` NÃO serve de filtro aqui: medido no acervo,
        as 43 mensagens com `extendedTextMessage.matchedText` estão todas
        marcadas como `messageType = 'conversation'` — a coluna reflete algo
        que não é o formato do JSON. `api.detalhes()` já sabia disso e olha
        direto para `message.extendedTextMessage`, sem checar o tipo; a query
        faz o mesmo, filtrando pelo CONTEÚDO, não pela coluna.

        `canonicalUrl` está sempre vazio neste acervo (medido: 0 em milhares de
        mensagens) — o WhatsApp preenche `matchedText`, a URL tal como o
        remetente digitou. A condição cobre os dois, para não depender de um
        campo que a versão atual do app nunca grava.
        """
        if not self.pg:
            raise LookupError("Postgres não configurado")
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("psycopg não instalado") from e
        try:
            with psycopg.connect(**self.pg, connect_timeout=5) as con:
                cur = con.execute(
                    """SELECT key, "pushName", "messageType", message, "messageTimestamp"
                       FROM "Message"
                       WHERE key->>'remoteJid' = ANY(%s)
                         AND (message->'extendedTextMessage' ? 'canonicalUrl'
                              OR message->'extendedTextMessage' ? 'matchedText')
                       ORDER BY "messageTimestamp" DESC LIMIT %s""",
                    (list(jids), limite),
                )
                return [
                    {"key": k, "pushName": n, "messageType": mt, "message": m,
                     "messageTimestamp": ts}
                    for k, n, mt, m, ts in cur.fetchall()
                ]
        except Exception as e:
            motivo = str(e).splitlines()[0]
            curto = "senha recusada" if "fe_sendauth" in motivo or "password" in motivo else (
                "sem rota até a porta" if "failed" in motivo else motivo[:44]
            )
            raise RuntimeError(f"Postgres: {curto}") from e

    def buscar_recente(self, termo: str, janela_s: int = JANELA, limite: int = 40) -> list[dict]:
        """Primeira onda da busca: só a janela recente. Usa o índice de tempo,
        volta em milissegundos e já cobre o que se procura na maioria das vezes."""
        return self._buscar(termo, limite, desde=int(time.time()) - janela_s)

    def buscar_antigas(self, termo: str, janela_s: int = JANELA, limite: int = 80) -> list[dict]:
        """Segunda onda: tudo que é mais velho que a janela."""
        return self._buscar(termo, limite, ate=int(time.time()) - janela_s)

    def _buscar(self, termo: str, limite: int, desde: int = 0, ate: int = 0) -> list[dict]:
        if not self.pg:
            raise LookupError("Postgres não configurado")
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("psycopg não instalado") from e
        recorte, args = "", [f"%{termo}%"]
        if desde:
            recorte = ' AND "messageTimestamp" >= %s'
            args.append(desde)
        elif ate:
            recorte = ' AND "messageTimestamp" < %s'
            args.append(ate)
        args.append(limite)
        try:
            with psycopg.connect(**self.pg, connect_timeout=5) as con:
                cur = con.execute(
                    f"""SELECT key, "pushName", "messageType", message, "messageTimestamp"
                        FROM "Message"
                        WHERE message->>'conversation' ILIKE %s{recorte}
                        ORDER BY "messageTimestamp" DESC LIMIT %s""",
                    tuple(args),
                )
                return [
                    {"key": k, "pushName": n, "messageType": mt,
                     "message": m, "messageTimestamp": ts}
                    for k, n, mt, m, ts in cur.fetchall()
                ]
        except Exception as e:
            raise RuntimeError(f"Postgres: {str(e).splitlines()[0][:60]}") from e

    def send_reaction(self, key: dict, emoji: str) -> dict:
        """emoji vazio remove a reação — é assim que o WhatsApp desfaz."""
        r = self.http.post(
            f"/message/sendReaction/{self.instance}",
            json={"key": key, "reaction": emoji},
        )
        r.raise_for_status()
        return r.json()

    def arquivar(self, chat: dict, arquivar: bool = True) -> dict:
        ultima = (chat.get("lastMessage") or {}).get("key") or {}
        r = self.http.post(
            f"/chat/archiveChat/{self.instance}",
            json={"chat": chat.get("remoteJid"), "lastMessage": {"key": ultima},
                  "archive": arquivar},
        )
        r.raise_for_status()
        return r.json()

    def marcar_nao_lida(self, chat: dict) -> dict:
        ultima = (chat.get("lastMessage") or {}).get("key") or {}
        r = self.http.post(
            f"/chat/markChatUnread/{self.instance}",
            json={"chat": chat.get("remoteJid"), "lastMessage": {"key": ultima}},
        )
        r.raise_for_status()
        return r.json()

    def send_file(
        self, number: str, path: pathlib.Path, caption: str = "",
        quoted: dict | None = None, gif: bool = False,
    ) -> dict:
        """`gif=True` pede que o vídeo chegue como GIF (loop automático).

        A 2.3.7 de fábrica IGNORA isso: `prepareMediaMessage` força
        `gifPlayback = false` para todo `mediatype: video`. Mandar o campo é
        inofensivo lá — ela sobrescreve — e passa a valer com o patch de uma
        linha descrito no README, que troca o `false` fixo por respeitar
        este campo. Sem o campo, o comportamento é idêntico ao de fábrica.
        """
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        else:
            # ponytail: audio como documento preserva o arquivo original.
            # PTT de verdade e' sendWhatsAppAudio, fase 2.
            kind = "document"
        r = self.http.post(
            f"/message/sendMedia/{self.instance}",
            json={
                "number": number,
                "mediatype": kind,
                "mimetype": mime,
                "fileName": path.name,
                "caption": caption,
                "media": base64.b64encode(path.read_bytes()).decode(),
                **({"gifPlayback": True} if gif and kind == "video" else {}),
                **({"quoted": {"key": quoted}} if quoted else {}),
            },
        )
        r.raise_for_status()
        return r.json()

    def avatar_file(self, chat: dict) -> pathlib.Path | None:
        """Foto de perfil em disco. None = contato sem foto, ou URL expirada."""
        jid = chat.get("remoteJid") or ""
        if not jid:
            return None
        destino = FOTOS / f"{jid.replace('/', '_')}.jpg"
        if destino.exists():
            return destino if destino.stat().st_size else None

        def fresca() -> str | None:
            try:
                r = self.http.post(
                    f"/chat/fetchProfilePictureUrl/{self.instance}", json={"number": jid}
                )
                return r.json().get("profilePictureUrl") if r.is_success else None
            except Exception:
                return None

        # A Evolution guarda a URL no banco e nunca a renova: medido, 52% das
        # que vem no findChats (e 64% das do findContacts) ja' expiraram e dao
        # 403 no CDN. Por isso a URL guardada e' so' a primeira tentativa.
        url = chat.get("profilePicUrl") or fresca()
        if not url:
            return None

        def baixar(u: str):
            try:
                return httpx.get(u, timeout=20, follow_redirects=True)
            except Exception:
                return None

        r = baixar(url)
        if (r is None or not r.is_success) and (nova := fresca()) and nova != url:
            r = baixar(nova)  # a guardada venceu; a fresca vale ~10 dias
        if r is None or not r.is_success or not r.content:
            return None
        try:
            destino.write_bytes(r.content)
        except Exception:
            return None
        return destino

    # ---------- mídia em disco ----------

    def media_file(self, msg: dict) -> pathlib.Path | None:
        """Baixa a mídia (se ainda não estiver em disco) e devolve o caminho.

        None = sem anexo, ou ponteiro expirado no CDN do WhatsApp. Não há como
        saber qual dos dois sem tentar, então não tentamos adivinhar antes.
        """
        if not media_kind(msg.get("messageType", "")):
            return None
        key = msg.get("key") or {}
        mid = key.get("id")
        if not mid:
            return None

        achados = list(MEDIA.glob(f"{mid}.*"))
        if achados:
            return achados[0]

        try:
            r = self.http.post(
                f"/chat/getBase64FromMediaMessage/{self.instance}",
                json={"message": {"key": key}},
            )
            if not r.is_success:  # a Evolution responde 201 aqui, nao 200
                return None
            d = r.json()
            bruto = base64.b64decode(d["base64"])
        except Exception:
            return None

        ext = pathlib.Path(d.get("fileName") or "").suffix
        ext = ext or mimetypes.guess_extension(d.get("mimetype", "")) or ".bin"
        destino = MEDIA / f"{mid}{ext}"
        destino.write_bytes(bruto)
        return destino


# As mesmas opções do WhatsApp. 0 = para sempre.
DURACOES_MUDO = [("8 horas", 8 * 3600), ("1 semana", 7 * 86400), ("Sempre", 0)]


def carregar_favoritas() -> dict[str, dict]:
    """ponytail: favoritar mensagem nao tem endpoint na Evolution. Guardamos a
    mensagem inteira, para a lista de favoritas funcionar sem ir ao servidor."""
    try:
        return json.loads(FAVORITAS.read_text())
    except Exception:
        return {}


def salvar_favoritas(mapa: dict[str, dict]) -> None:
    FAVORITAS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(FAVORITAS, mapa, ensure_ascii=False, indent=2)


def carregar_silenciados() -> dict[str, int]:
    """{jid: epoch em que o silêncio acaba}; 0 = para sempre.

    ponytail: a Evolution 2.3.7 nao persiste o `muteEndTime` que o Baileys
    recebe do WhatsApp, e nao ha endpoint para consultar. O que der para
    capturar do evento chats.* e' importado; o resto e' escolha local.
    """
    try:
        bruto = json.loads(SILENCIADOS.read_text())
    except Exception:
        return {}
    if isinstance(bruto, list):  # formato antigo: lista de jids, tudo "sempre"
        return {j: 0 for j in bruto}
    return {k: int(v) for k, v in bruto.items()}


def salvar_silenciados(mapa: dict[str, int]) -> None:
    SILENCIADOS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(SILENCIADOS, mapa, indent=2)


def esta_mudo(mapa: dict[str, int], jid: str, agora: float | None = None) -> bool:
    """Silêncio com prazo: expira sozinho, sem faxina."""
    if jid not in mapa:
        return False
    fim = mapa[jid]
    if fim == 0:
        return True
    return (agora if agora is not None else time.time()) < fim


def importar_mute(chat: dict, mapa: dict[str, int]) -> bool:
    """Traz o `muteEndTime` do WhatsApp quando a Evolution o repassa.
    Devolve True se mudou alguma coisa."""
    jid = chat.get("remoteJid") or chat.get("id") or ""
    bruto = chat.get("muteEndTime", chat.get("mute"))
    if not jid or bruto in (None, ""):
        return False
    fim = _num(bruto)
    if fim > 1e11:  # veio em milissegundos
        fim //= 1000
    novo = 0 if fim < 0 else fim
    if mapa.get(jid) == novo:
        return False
    if novo == 0 and fim == 0:
        mapa.pop(jid, None)
    else:
        mapa[jid] = novo
    return True


def carregar_ocultos() -> set[str]:
    """ponytail: 'apagar para mim' nao existe na API do WhatsApp. Guardamos os
    ids escondidos aqui — o servidor continua tendo a mensagem, voce nao a ve."""
    try:
        return set(json.loads(OCULTOS.read_text()))
    except Exception:
        return set()


def salvar_ocultos(ids: set[str]) -> None:
    OCULTOS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(OCULTOS, sorted(ids), indent=2)


def carregar_apelidos() -> dict[str, str]:
    """ponytail: o Baileys nao sincroniza a agenda do celular, entao o nome que
    aparece e' o pushName que a pessoa escolheu. Aqui voce manda no nome."""
    try:
        return json.loads(APELIDOS.read_text())
    except Exception:
        return {}


def salvar_apelidos(mapa: dict[str, str]) -> None:
    APELIDOS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(APELIDOS, mapa, indent=2, ensure_ascii=False)


def carregar_fixados() -> list[str]:
    """ponytail: preferencia de UI em um JSON. Fixar conversa nao existe na API
    do WhatsApp — e' escolha sua, mora na sua maquina."""
    try:
        return json.loads(FIXADOS.read_text())
    except Exception:
        return []


def salvar_fixados(jids: list[str]) -> None:
    FIXADOS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(FIXADOS, jids, indent=2)


def carregar_emojis_recentes() -> list[str]:
    """Chars de emoji usados no seletor — ordem de uso, mais recente primeiro."""
    try:
        dados = json.loads(RECENTES_EMOJI.read_text())
        return dados if isinstance(dados, list) else []
    except Exception:
        return []


def salvar_emojis_recentes(chars: list[str]) -> None:
    RECENTES_EMOJI.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(RECENTES_EMOJI, chars, ensure_ascii=False)


def carregar_hd_ids() -> set[str]:
    """Ids de imagens que ENVIAMOS em HD.

    A Evolution 2.3.7 nunca grava midQualityFileSha256 no payload (testado com
    envio controlado de 3000px) — o selo HD das nossas enviadas precisa de
    marca local, como favoritas/ocultos.
    """
    try:
        dados = json.loads(HD_ENVIADAS.read_text())
        return set(dados) if isinstance(dados, list) else set()
    except Exception:
        return set()


def salvar_hd_ids(ids: set[str]) -> None:
    HD_ENVIADAS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(HD_ENVIADAS, sorted(ids), ensure_ascii=False)


def carregar_arquivadas() -> set[str]:
    """Arquivadas ficam em marca local: o findChats da Evolution 2.3.7 NÃO
    devolve o campo archived (medido em 609 chats) — o servidor só aceita
    archiveChat, e o estado some ao reordenar/recarregar a sidebar."""
    try:
        dados = json.loads(ARQUIVADAS.read_text())
        return set(dados) if isinstance(dados, list) else set()
    except Exception:
        return set()


def salvar_arquivadas(ids: set[str]) -> None:
    ARQUIVADAS.parent.mkdir(parents=True, exist_ok=True)
    gravar_json(ARQUIVADAS, sorted(ids), ensure_ascii=False)


# ---------- Tenor (seletor de GIF, como o WhatsApp Web usa Giphy) ----------

@functools.lru_cache(maxsize=64)
def _giphy(termo: str, chave: str, limite: int) -> tuple:
    """Uma chamada por termo POR SESSAO.

    A key beta do Giphy da' 100 chamadas/hora. Sem cache, abrir o seletor tres
    vezes e digitar duas buscas ja' come 5 — e o debounce do campo dispara mais.
    Resultado de busca nao muda em minutos; guardar a sessao inteira e' barato.
    """
    busca = bool(termo.strip())
    url = f"https://api.giphy.com/v1/gifs/{'search' if busca else 'trending'}"
    params = {
        "api_key": chave,
        "limit": limite,
        # so' as renditions que este uso precisa — o padrao traz ~14
        "bundle": "messaging_non_clips",
        "lang": "pt",
    }
    if busca:
        params["q"] = termo.strip()[:50]   # a API corta em 50 e devolve 414
    r = httpx.get(url, params=params, timeout=15)
    r.raise_for_status()
    return tuple(r.json().get("data", []) or [])


def giphy_buscar(termo: str, chave: str, limite: int = 24) -> list[dict]:
    """Resultados do Giphy (mp4 + prévia). Sem termo, traz o trending.

    Levanta HTTPError com 401/403 quando a key é inválida e 429 quando estoura
    a cota — o popup mostra o erro amigável em vez de parecer que não há GIFs.

    Trocado do Tenor em 09/09/2026: a API do Tenor foi desligada em 30/06/2026
    e não aceita novas chaves desde janeiro. (E o código do Tenor nunca chegou a
    funcionar: batia em `tenor.com`, que é o site, não em `tenor.googleapis.com`.)
    """
    if not chave:
        raise LookupError("sem key do Giphy — cole no ⚙ Configuração")
    saida = []
    for it in _giphy(termo, chave, limite):
        im = it.get("images") or {}
        # Enviar na maior qualidade, como a doc recomenda. O bundle
        # messaging_non_clips traz `original` (que TEM mp4), nao `original_mp4`
        # — sem esta ordem cai no fixed_width de 200px.
        cheio = im.get("original") or {}
        mp4 = (cheio.get("mp4")
               or (im.get("original_mp4") or {}).get("mp4")
               or (im.get("fixed_width") or {}).get("mp4") or "")
        # grade: 100px de largura, o que a doc recomenda para preview
        prev = ((im.get("fixed_width_small") or {}).get("url")
                or (im.get("fixed_width") or {}).get("url") or "")
        if not mp4:
            continue
        cx = cheio or im.get("original_mp4") or im.get("fixed_width") or {}
        saida.append({
            "id": it.get("id"), "mp4": mp4, "prev": prev,
            # o Giphy manda dimensao como STRING
            "larg": _num(cx.get("width")), "alt": _num(cx.get("height")),
            "tags": [], "titulo": (it.get("title") or "")[:80],
        })
    return saida


def gif_baixar(url: str) -> pathlib.Path | None:
    """Baixa o mp4 do provedor para a pasta de GIFs (nome estável por URL).

    A URL vai inteira, com os query params: a doc do Giphy proíbe modificá-la.
    """
    import hashlib

    GIFS.mkdir(parents=True, exist_ok=True)
    destino = GIFS / f"gif_{hashlib.sha1(url.encode()).hexdigest()[:12]}.mp4"
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        destino.write_bytes(r.content)
        return destino
    except Exception:
        return None


PICOS_CACHE: dict[str, list[float]] = {}


def picos_de_audio(caminho: str | pathlib.Path) -> list[float] | None:
    """Envelope RMS do áudio local em ~48 pontos (onda de verdade na bolha).

    O opus é decodificado para PCM 8 kHz mono via ffmpeg e o RMS de cada
    janela vira a altura de uma barra. Cache em memória por (arquivo, mtime);
    None quando não dá (sem arquivo, sem ffmpeg) — o desenho cai no hash.
    """
    p = pathlib.Path(caminho) if caminho else None
    if not p or not p.exists():
        return None
    chave = f"{p}|{p.stat().st_mtime_ns}"
    if chave in PICOS_CACHE:
        return PICOS_CACHE[chave]
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(p), "-f", "s16le", "-ac", "1",
             "-ar", "8000", "-"],
            capture_output=True, timeout=25,
        )
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    amostras = memoryview(r.stdout).cast("h")
    if not len(amostras):
        return None
    NB = 48
    janela = max(1, len(amostras) // NB)
    picos = []
    for i in range(NB):
        bloco = amostras[i * janela:(i + 1) * janela]
        if not len(bloco):
            break
        soma = sum(int(a) * int(a) for a in bloco) / len(bloco)
        picos.append((soma ** 0.5) / 32768.0)
    maior = max(picos) or 1.0
    picos = [min(1.0, v / maior * 1.2) for v in picos]
    PICOS_CACHE[chave] = picos
    return picos


def instante(chat: dict) -> int:
    """Momento da última atividade, em epoch. `lastMessage` manda; `updatedAt`
    é o reserva, porque a Evolution nem sempre atualiza os dois juntos."""
    ts = (chat.get("lastMessage") or {}).get("messageTimestamp")
    if ts:
        return _num(ts)
    quando = chat.get("updatedAt") or ""
    try:
        return int(
            datetime.fromisoformat(quando.replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, AttributeError):
        return 0


def ordenar(chats: list[dict], fixados: list[str]) -> list[dict]:
    """Fixados no topo, na ordem em que foram fixados; o resto por atividade,
    do mais recente para o mais antigo.

    Ordenar só a lista visível não basta: qualquer filtro ou busca reconstrói a
    view a partir da lista completa, e a ordem antiga voltaria.
    """
    posicao = {jid: i for i, jid in enumerate(fixados)}

    def chave(c: dict):
        jids = c.get("_jids") or [c.get("remoteJid", "")]
        fixo = min((posicao[j] for j in jids if j in posicao), default=None)
        if fixo is not None:
            return (0, fixo, 0)
        return (1, 0, -instante(c))

    return sorted(chats, key=chave)


if __name__ == "__main__":
    # ponytail: o unico check da Fase 1. Falha se a API mudar de formato.
    api = Api(load_config())

    chats = api.chats()
    assert chats and "remoteJid" in chats[0], "findChats mudou de formato"
    print(f"ok  {len(chats)} chats — topo: {chats[0].get('pushName')}")

    jid = chats[0]["remoteJid"]
    p1, paginas = api.messages(jid, page=1, size=50)
    assert len(p1) <= 50, "offset nao e' o tamanho da pagina"
    ts = [m["messageTimestamp"] for m in p1]
    assert ts == sorted(ts, reverse=True), "ordem deixou de ser nova->velha"
    print(f"ok  {len(p1)} msgs de {paginas} páginas, ordem DESC")

    if paginas > 1:
        p2, _ = api.messages(jid, page=2, size=50)
        repetidas = {m["key"]["id"] for m in p1} & {m["key"]["id"] for m in p2}
        assert len(p2) > len(repetidas), "page=2 nao trouxe nada novo"
        # a paginacao e' por offset: mensagem nova entre uma chamada e outra
        # desloca tudo e faz a pagina seguinte repetir. Quem protege a tela e'
        # a deduplicacao em Mensagens.anteriores() — aqui so' registramos.
        print(f"ok  paginação avança ({len(repetidas)} repetidas pelo offset, "
              f"deduplicadas na inserção)")

    assert text_of({"message": {"conversation": "oi"}}) == "oi"
    assert text_of({"message": {"locationMessage": {"name": "Recife",
        "address": "PE"}}}) == "📍 Recife — PE", "location perde nome/endereco"
    assert text_of({"message": {"locationMessage": {}}}) == "📍 Localização"
    assert text_of({"message": {"contactMessage": {"displayName": "Ana"}}}) == "👤 Ana"
    poll = text_of({"message": {"pollCreationMessage": {"name": "P?",
        "options": [{"optionName": "A"}, {"optionName": "B"}]}}})
    assert poll == "📊 P?\n• A\n• B", f"poll: {poll!r}"
    assert not e_visivel({"messageType": "templateMessage"}), "template vira bolha vazia"
    assert not e_visivel({"messageType": "albumMessage"})
    assert text_of({"key": {"deleted": True}, "message": {"conversation": "segredo"}}) == APAGADA, (
        "conteudo de mensagem apagada nao pode vazar"
    )
    assert foi_apagada({"status": "DELETED"}) and not foi_apagada({"key": {}})
    print("ok  mensagem apagada não mostra o conteúdo")
    assert "📄" in text_of(
        {"messageType": "documentMessage", "message": {"documentMessage": {"fileName": "x.pdf"}}}
    )
    assert media_kind("conversation") is None and media_kind("imageMessage") == "image"
    print("ok  text_of / media_kind")

    brutas = [
        {"key": {"id": "m1"}, "messageType": "conversation", "message": {"conversation": "oi"}},
        {"key": {"id": "r1", "participant": "ana@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "👍"}}},
        {"key": {"id": "p1"}, "messageType": "protocolMessage", "message": {}},
    ]
    limpas = aplicar_reacoes(brutas)
    assert [m["key"]["id"] for m in limpas] == ["m1"], "reação/protocolo viraram bolha"
    assert limpas[0]["_reacoes"] == {"ana@lid": "👍"}
    removida = aplicar_reacoes([
        brutas[0],
        {"key": {"id": "r2", "participant": "ana@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": ""}}},
    ])
    assert "_reacoes" not in removida[0], "reação removida continuou aparecendo"

    # duas pessoas com o MESMO emoji contam duas
    duas = aplicar_reacoes([
        brutas[0],
        {"key": {"id": "rA", "participant": "ana@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "👍"}}},
        {"key": {"id": "rB", "participant": "duda@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "👍"}}},
    ])
    assert resumo_reacoes(duas[0]) == ("👍", 2), resumo_reacoes(duas[0])

    # a mesma pessoa trocando de emoji conta UMA, a última.
    # a lista chega do mais novo para o mais velho, como a API devolve:
    trocou = aplicar_reacoes([
        {"key": {"id": "r2", "participant": "ana@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "❤️"}}},
        {"key": {"id": "r1", "participant": "ana@lid"}, "messageType": "reactionMessage",
         "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "😀"}}},
        brutas[0],
    ])
    assert resumo_reacoes(trocou[0]) == ("❤️", 1), resumo_reacoes(trocou[0])

    # minha reacao nao colide com a do contato em conversa 1:1
    assert reacao_de({"key": {"fromMe": True, "remoteJid": "z@s.whatsapp.net"},
                      "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "🔥"}}}
                     )[2] == "eu"
    assert reacao_de({"key": {"fromMe": False, "remoteJid": "z@s.whatsapp.net"},
                      "message": {"reactionMessage": {"key": {"id": "m1"}, "text": "🔥"}}}
                     )[2] == "z@s.whatsapp.net"
    print("ok  reações contadas por pessoa, não por emoji")

    assert formatar("*BRASIL* comum") == "<b>BRASIL</b> comum"
    assert formatar("_ita_") == "<i>ita</i>"
    assert formatar("~ris~") == "<s>ris</s>"
    assert "<b>" not in formatar("2*3*4"), "asterisco entre numeros nao e' negrito"
    assert "<i>" not in formatar("nome_do_arquivo_final"), "underscore no meio de palavra"
    assert "<code>" in formatar("use `pip install`")
    assert "<pre" in formatar("```bloco\nlinha```")
    assert "<b>" not in formatar("```*nao vira negrito*```"), "mono e' literal"
    assert "•" in formatar("- item"), "lista com hifen"
    assert "1." in formatar("1. primeiro")
    assert "border-left" in formatar("> citado"), "citacao com barra"
    assert "<a href=" in formatar("veja https://g1.globo.com/x.html")
    assert "&lt;script&gt;" in formatar("<script>"), "html do remetente tem de ser escapado"
    assert formatar("*a* e _b_") == "<b>a</b> e <i>b</i>"
    print("ok  formatação do WhatsApp: negrito, itálico, riscado, mono, listas, citação, link")

    assert formatar_numero("558133334444@s.whatsapp.net") == "+55 81 3333-4444"
    assert formatar_numero("123456789012345@lid") == "Contato", (
        "LID nao e' telefone: nao pode virar +123456789012345"
    )
    assert formatar_numero("987654321098765") == "Contato"
    assert montar_numero("55", "81 99999-0000") == "5581999990000"
    assert montar_numero("+55", "(81) 9999-90000") == "5581999990000"
    assert montar_numero("55", "5581999990000") == "5581999990000", "não duplica o DDI"
    assert montar_numero("", "81999") == "" and montar_numero("55", "") == ""
    assert bandeira("BR") == "🇧🇷" and bandeira("PT") == "🇵🇹"
    assert bandeira("br") == "🇧🇷", "minúsculo também vale"
    assert bandeira("") == "🏳" and bandeira("BRA") == "🏳" and bandeira("5") == "🏳"
    assert all(len(p) == 3 and len(p[0]) == 2 for p in PAISES)
    assert nome_do_chat({"remoteJid": "x@lid", "pushName": "Ana"}) == "Ana"
    assert nome_do_chat(
        {"remoteJid": "x@lid", "lastMessage": {"pushName": "Você", "key": {"fromMe": True,
         "remoteJidAlt": "558133334444@s.whatsapp.net"}}}
    ) == "+55 81 3333-4444", "pushName 'Você' nao pode virar nome do contato"
    assert nome_do_chat(
        {"remoteJid": "x@lid", "lastMessage": {"pushName": "Duda", "key": {"fromMe": False}}}
    ) == "Duda"
    print("ok  nome_do_chat / formatar_numero")

    mapa = api.contatos()
    resolvidos = sum(1 for c in chats if not nome_do_chat(c, mapa).startswith(("+", "5", "1", "2")))
    print(f"ok  {resolvidos}/{len(chats)} chats com nome de verdade")

    unidos = unir_duplicados(chats, mapa)
    assert len(unidos) <= len(chats)
    assert all("_jids" in c for c in unidos)
    print(f"ok  {len(chats)} chats -> {len(unidos)} contatos ({len(chats)-len(unidos)} duplicatas unidas)")

    par = [
        {"remoteJid": "9@lid", "updatedAt": "2026-01-02",
         "lastMessage": {"key": {"remoteJidAlt": "5581@s.whatsapp.net"}}},
        {"remoteJid": "5581@s.whatsapp.net", "pushName": "Carla", "updatedAt": "2026-01-01"},
    ]
    u = unir_duplicados(par)
    assert len(u) == 1 and u[0]["pushName"] == "Carla", "nome bom do chat menos ativo se perdeu"
    assert set(u[0]["_jids"]) == {"9@lid", "5581@s.whatsapp.net"}
    print("ok  união preserva o melhor nome e os dois JIDs")

    m, a_, x = previa({"remoteJid": "z@s.whatsapp.net", "lastMessage": {
        "key": {"fromMe": True}, "status": "READ", "messageType": "conversation",
        "message": {"conversation": "oi"}}})
    assert (m, a_, x) == ("✓✓", "Você", "oi"), (m, a_, x)
    m, a_, x = previa({"remoteJid": "g@g.us", "lastMessage": {
        "key": {"fromMe": False}, "pushName": "Wilton", "messageType": "imageMessage",
        "message": {"imageMessage": {"caption": "No cliente"}}}})
    assert (m, a_, x) == ("", "Wilton", "🖼 No cliente"), (m, a_, x)
    m, a_, x = previa({"remoteJid": "z@s.whatsapp.net", "lastMessage": {
        "key": {"fromMe": True}, "messageType": "audioMessage",
        "message": {"audioMessage": {"seconds": 76}}}})
    assert x == "🎤 1:16", x
    assert detalhes({"messageType": "conversation",
                     "message": {"conversation": "texto puro"}}) == {"tipo": "conversation"}, (
        "message['conversation'] e' string: nao pode ser tratada como dict"
    )
    det_link = detalhes({"messageType": "extendedTextMessage", "message": {
        "extendedTextMessage": {"title": "T", "matchedText": "https://x.com/a",
                                "jpegThumbnail": "YQ=="}}})
    assert det_link["link"]["thumb"] == "YQ==", "thumb do preview de link se perdeu"
    assert det_link["link"]["url"] == "https://x.com/a"
    assert detalhes({"messageType": "imageMessage", "message": {"imageMessage": {
        "midQualityFileSha256": "abc"}}})["hd"] is True, "imagem HD nao marcada"
    assert detalhes({"messageType": "imageMessage",
                     "message": {"imageMessage": {"width": 100}}})["hd"] is False
    assert _num({"low": 69, "high": 0}) == 69 and _num(42) == 42 and _num(None) == 0

    eu = {"key": {"fromMe": True}}
    assert status_de({**eu, "MessageUpdate": [
        {"status": "SERVER_ACK"}, {"status": "DELIVERY_ACK"}, {"status": "SERVER_ACK"}
    ]}) == "DELIVERY_ACK", "a lista vem fora de ordem: vale o mais avancado"
    assert marca_de({**eu, "MessageUpdate": [{"status": "READ"}]}) == ("✓✓", True)
    assert marca_de({**eu, "MessageUpdate": [{"status": "DELIVERY_ACK"}]}) == ("✓✓", False)
    assert marca_de({**eu, "status": "SERVER_ACK"}) == ("✓", False)
    assert marca_de({**eu, "status": "PENDING"}) == ("🕐", False)
    assert marca_de({**eu, "MessageUpdate": []}) == ("✓", False)
    assert marca_de({"key": {"fromMe": False}}) == ("", False), "recebida nao tem marca"
    assert foi_editada({"MessageUpdate": [{"status": "EDITED"}]})
    print("ok  checkmarks: ✓ enviado, ✓✓ entregue, ✓✓ azul lido")

    agora = 1_000_000
    mapa = {"sempre@x": 0, "expirado@x": agora - 10, "ativo@x": agora + 3600}
    assert esta_mudo(mapa, "sempre@x", agora)
    assert not esta_mudo(mapa, "expirado@x", agora), "silencio vencido tem de acabar sozinho"
    assert esta_mudo(mapa, "ativo@x", agora)
    assert not esta_mudo(mapa, "nunca@x", agora)
    m2: dict[str, int] = {}
    assert importar_mute({"remoteJid": "g@g.us", "muteEndTime": 1_700_000_000_000}, m2)
    assert m2["g@g.us"] == 1_700_000_000, "muteEndTime em ms precisa virar segundos"
    assert not importar_mute({"remoteJid": "g@g.us", "muteEndTime": 1_700_000_000_000}, m2)
    print("ok  silêncio com prazo e importação de muteEndTime")
    print("ok  prévia da barra lateral no formato do WhatsApp Web")

    amostra = [
        {"remoteJid": "a", "lastMessage": {"messageTimestamp": 100}},
        {"remoteJid": "b", "lastMessage": {"messageTimestamp": 300}},
        {"remoteJid": "c", "lastMessage": {"messageTimestamp": 200}},
    ]
    assert [c["remoteJid"] for c in ordenar(amostra, [])] == ["b", "c", "a"], (
        "sem fixados, manda a atividade mais recente"
    )
    assert [c["remoteJid"] for c in ordenar(amostra, ["a"])] == ["a", "b", "c"], (
        "fixado fica no topo e o resto segue por atividade"
    )
    amostra[0]["lastMessage"]["messageTimestamp"] = 999
    assert [c["remoteJid"] for c in ordenar(amostra, [])] == ["a", "b", "c"], (
        "mensagem nova tem de subir o chat"
    )
    assert instante({"updatedAt": "2026-09-08T20:22:25.000Z"}) > 0
    print("ok  ordem: fixados no topo, resto por atividade")
    print("ok  ordenar com fixados")

    # gravar_json troca o arquivo inteiro: um leitor concorrente ve o velho ou
    # o novo, nunca meio JSON. E' o que impedia o estado local de sumir calado.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        alvo = pathlib.Path(d) / "estado.json"
        gravar_json(alvo, ["a", "b"], indent=2)
        assert json.loads(alvo.read_text()) == ["a", "b"]
        gravar_json(alvo, ["c"], indent=2)
        assert json.loads(alvo.read_text()) == ["c"], "regravar tem de substituir"
        assert not alvo.with_suffix(".tmp").exists(), "o .tmp nao pode sobrar"
        # o pai inexistente e' criado, senao fixar a 1a conversa perdia a marca
        fundo = pathlib.Path(d) / "sub" / "dir" / "x.json"
        gravar_json(fundo, {"k": 1})
        assert json.loads(fundo.read_text()) == {"k": 1}
    print("ok  gravar_json troca o arquivo inteiro (sem meio-JSON, sem .tmp orfao)")

    # config truncado tem de dar recado, nao traceback: a apikey mora nele e o
    # Ctrl+= regrava a cada passo de zoom.
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "read_text", lambda *_: 'host = "http'):
        try:
            load_config()
            raise AssertionError("config corrompido passou batido")
        except SystemExit as e:
            assert "corrompido" in str(e), f"recado errado: {e}"
    print("ok  config corrompido vira recado, não traceback")

    # privacidade exige as 6 chaves: a rota aplica todas incondicionalmente e
    # chave ausente chega como `undefined` no Baileys (lido na fonte do 2.3.7)
    completo = {k: v[1][0] for k, v in Api.PRIVACIDADE.items()}
    try:
        Api.privacidade_definir(api, dict(list(completo.items())[:5]))
        raise AssertionError("aceitou conjunto incompleto")
    except ValueError as e:
        assert "faltam" in str(e), e
    print("ok  privacidade recusa conjunto incompleto (chave ausente = undefined)")

    # valor que a lista do Baileys nao previa nao pode ser dropado: o servidor
    # foi quem devolveu, e devolver intacto e' mais seguro que sumir com a chave
    herdado = dict(completo); herdado["online"] = "modo_futuro_do_whatsapp"
    assert set(herdado) == set(Api.PRIVACIDADE), "conjunto perdeu chave"
    print("ok  valor desconhecido mantém a chave no conjunto")

    # gif=True so' acrescenta o campo em video; nos outros tipos nao faz nada.
    # Isto guarda o patch de uma linha da instancia (ver README): mandar o
    # campo em imagem/documento seria ruido, e nao manda-lo em video mataria a
    # unica coisa que o patch destrava.
    class _HttpFalso:
        def __init__(self):
            self.corpo = None

        def post(self, _url, json=None, **_k):
            self.corpo = json

            class R:
                is_success, content = True, b"{}"
                @staticmethod
                def raise_for_status(): pass
                @staticmethod
                def json(): return {}
            return R()

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        falso = Api.__new__(Api)
        falso.instance, falso.me = "t", "5581"
        for nome, flag, esperado in (("a.mp4", True, True), ("a.mp4", False, False),
                                     ("a.jpg", True, False), ("a.pdf", True, False)):
            alvo = pathlib.Path(d) / nome
            alvo.write_bytes(b"x")
            falso.http = _HttpFalso()
            Api.send_file(falso, "5581@s.whatsapp.net", alvo, gif=flag)
            tem = "gifPlayback" in (falso.http.corpo or {})
            assert tem is esperado, f"{nome} gif={flag}: presente={tem}, esperava {esperado}"
    print("ok  gifPlayback só vai no corpo quando é vídeo E gif=True")
