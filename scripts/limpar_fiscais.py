"""
Limpeza dos fiscais de contratos - Portal de Fiscais de Contratos (CGE-MG)

Lê as planilhas fiscais_contratos_AAAA.xlsx da pasta `upload` e devolve a coluna
"Fiscais do Contrato" no formato padrão:

    Nome Completo - MASP 1234567-8, Outro Nome - MASP 7654321-0

Uso:
    python limpar_fiscais.py <pasta_upload> <arquivo_saida.xlsx>

O que o script faz:
  1. Remove lixo de formatação (_x000D_, quebras de linha, tabs, espaços duplos).
  2. Troca travessões (– —) por hífen e padroniza "MASP:", "Masp", "-MASP" etc.
  3. Separa cada fiscal, extrai nome e MASP/Matrícula e reescreve no padrão.
  4. Ajusta o nome (maiúsculas/minúsculas, "de/da/dos" em minúsculo).
  5. Quando um fiscal está sem MASP, procura o mesmo nome em outros contratos
     (de qualquer ano) e, se houver um único MASP para ele, completa e sinaliza
     para conferência.
  6. Marca o que não deu para resolver como "Pendente", com o motivo.
"""
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

PARTICULAS = {"de", "da", "do", "das", "dos", "e", "d", "di", "du", "del", "van", "von"}
ROMANOS = {"ii", "iii", "iv"}

ST_OK = "OK - já estava correto"
ST_CORR = "Corrigido automaticamente"
ST_COMPL = "MASP completado (conferir)"
ST_PEND = "Pendente"
ST_VAZIO = "Sem fiscal"

# ---------------------------------------------------------------- utilidades


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def chave_nome(nome: str) -> str:
    return re.sub(r"\s+", " ", sem_acento(nome).lower()).strip()


def limpar_texto(s: str) -> str:
    s = str(s)
    s = re.sub(r"_x000[dD]_", " ", s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ").replace("\xa0", " ")
    s = re.sub(r"[\u2013\u2014\u2012\x96]", "-", s)  # travessões -> hífen
    s = s.replace("´", "'").replace("`", "'")
    return re.sub(r"\s+", " ", s).strip()


def formatar_nome(nome: str) -> str:
    nome = re.sub(r"[\[\]\(\)\"]", " ", nome)
    nome = re.sub(r"^[\s\-–:.;]+|[\s\-–:.;]+$", "", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    palavras = []
    for i, p in enumerate(nome.split(" ")):
        lp = p.lower()
        if i > 0 and lp in PARTICULAS:
            palavras.append(lp)
        elif lp in ROMANOS:
            palavras.append(lp.upper())
        else:
            # trata nomes com apóstrofo/hífen: D'Ávila, Ana-Maria
            palavras.append(re.sub(r"(^|['\-])(\w)", lambda m: m.group(1) + m.group(2).upper(), lp))
    return " ".join(palavras)


RE_ROTULO = re.compile(r"\b(masp|m[aá]tr[ií]cula|matric\.?|mat\.)\s*[:nº°.]*\s*", re.I)
RE_NUMERO = re.compile(r"\[?\s*[mM]?\s*(\d[\d.\s]*\d(?:\s*-\s*\d)?|\d)\s*\]?")


def normalizar_numero(bruto: str):
    """Devolve (numero_formatado, problema). numero_formatado = '1234567-8'."""
    bruto = bruto.strip()
    if "-" in bruto:
        base, dv = bruto.rsplit("-", 1)
        base = re.sub(r"\D", "", base)
        dv = re.sub(r"\D", "", dv)
        if len(dv) != 1 or not base:
            return None, "número ilegível"
    else:
        m = re.match(r"^([\d.\s]*\d)[.\s](\d)$", bruto)  # ex.: 105.545.8
        digitos = re.sub(r"\D", "", bruto)
        if m and len(re.sub(r"\D", "", m.group(1))) <= 7:
            base, dv = re.sub(r"\D", "", m.group(1)), m.group(2)
        elif len(digitos) == 8:  # 13546312 -> 1354631-2
            base, dv = digitos[:-1], digitos[-1]
        else:
            return f"{digitos}", "número sem dígito verificador"
    base = base.lstrip("0") or "0"
    if len(base) > 7:
        return f"{base}-{dv}", "número com dígitos a mais"
    return f"{base}-{dv}", None


def separar_fiscais(texto: str):
    partes = [p.strip() for p in re.split(r"[,;]|\s+/\s+", texto) if p.strip()]
    # "MASP 1.119.633-4 e FLÁVIO JOSÉ LOPES" -> separa no " e " quando há número antes
    saida = []
    for p in partes:
        m = re.match(r"^(.*\d)\s+e\s+([A-Za-zÀ-ÿ].*)$", p)
        if m:
            saida += [m.group(1), m.group(2)]
        else:
            saida.append(p)
    return saida


def interpretar_parte(parte: str):
    """Devolve dict(nome, tipo, numero, problema) ou None se a parte for vazia."""
    tipo, numero, problema = None, None, None
    m = RE_ROTULO.search(parte)
    if m:
        tipo = "MASP" if m.group(1).lower() == "masp" else "Matrícula"
        resto = parte[m.end():]
        mn = RE_NUMERO.match(resto)
        nome = parte[: m.start()] + " " + (resto[mn.end():] if mn else resto)
        if mn:
            numero, problema = normalizar_numero(mn.group(1))
        else:
            problema = "rótulo MASP sem número"
    else:
        mn = re.search(r"\s*-?\s*(\d[\d.\s]*-\s*\d|\d{6,8})\s*$", parte)
        if mn:
            tipo = "MASP"
            numero, problema = normalizar_numero(mn.group(1))
            nome = parte[: mn.start()]
        else:
            nome = parte
    nome = formatar_nome(re.sub(r"\d", "", nome))
    if not nome and not numero:
        return None
    return {"nome": nome, "tipo": tipo, "numero": numero, "problema": problema}


def montar(f):
    return f"{f['nome']} - {f['tipo']} {f['numero']}"


# ---------------------------------------------------------------- leitura


def ler_planilhas(pasta: Path) -> pd.DataFrame:
    tabelas = []
    arquivos = sorted(a for a in pasta.glob("fiscais_contratos_*.xlsx") if not a.name.startswith("~$"))
    if not arquivos:
        sys.exit(f"Nenhuma planilha fiscais_contratos_AAAA.xlsx encontrada em: {pasta.resolve()}\n"
                 "Informe a pasta certa, por exemplo:  python limpar_fiscais.py \"G:\\Meu Drive\\...\\upload\"")
    for arq in arquivos:
        print(f"Lendo {arq.name}...")
        ano = int(re.search(r"(\d{4})", arq.stem).group(1))
        df = pd.read_excel(arq, dtype=str)
        cols = {chave_nome(c).replace("_", " "): c for c in df.columns}

        def col(prefixo):
            return next(v for k, v in cols.items() if k.startswith(prefixo))

        tabelas.append(pd.DataFrame({
            "Ano": ano,
            "Órgão": df[col("orgaos participantes")].fillna("").str.strip(),
            "Número do contrato": df[col("numero do contrato")].fillna("").str.strip(),
            "Fornecedor": df[col("nome fornecedor")].fillna("").str.strip(),
            "Fiscais (original)": df[col("fiscais do contrato")].fillna("").astype(str),
        }))
    return pd.concat(tabelas, ignore_index=True)


# ---------------------------------------------------------------- principal


def processar(base: pd.DataFrame):
    # 1ª passada: interpreta tudo
    interpretados = []
    for txt in base["Fiscais (original)"]:
        limpo = limpar_texto(txt)
        lista = []
        for parte in separar_fiscais(limpo):
            f = interpretar_parte(parte)
            if f is None:
                continue
            # parte só com número ("MASP: [1071204-0]") -> pertence ao fiscal anterior
            if not f["nome"] and lista and not lista[-1]["numero"]:
                lista[-1].update(tipo=f["tipo"], numero=f["numero"], problema=f["problema"])
                continue
            lista.append(f)
        interpretados.append(lista)

    # dicionário nome -> números válidos encontrados em qualquer ano
    por_nome = defaultdict(set)
    for lista in interpretados:
        for f in lista:
            if f["nome"] and f["numero"] and not f["problema"]:
                por_nome[chave_nome(f["nome"])].add((f["tipo"], f["numero"]))

    linhas_contrato, linhas_fiscal = [], []
    for (_, row), lista, in zip(base.iterrows(), interpretados):
        original = limpar_texto(row["Fiscais (original)"])
        vistos, finais, pendencias, completou = set(), [], [], False
        for f in lista:
            k = chave_nome(f["nome"])
            if k and k in vistos:
                continue  # fiscal repetido no mesmo contrato
            vistos.add(k)
            origem = "Informado"
            if not f["nome"]:
                f["problema"] = "número sem nome"
            elif len(f["nome"].split()) < 2:
                f["problema"] = f["problema"] or "nome incompleto"
            if f["nome"] and not f["numero"]:
                achados = por_nome.get(k, set())
                if len(achados) == 1:
                    f["tipo"], f["numero"] = next(iter(achados))
                    origem, completou = "Completado de outro contrato", True
                elif len(achados) > 1:
                    f["problema"] = "nome com mais de um MASP em outros contratos"
                else:
                    f["problema"] = "sem MASP"
            if f["problema"]:
                pendencias.append(f"{f['nome'] or '?'}: {f['problema']}")
            finais.append(f)
            linhas_fiscal.append({
                "Ano": row["Ano"], "Órgão": row["Órgão"],
                "Número do contrato": row["Número do contrato"],
                "Nome do fiscal": f["nome"], "Tipo": f["tipo"] or "",
                "MASP / Matrícula": f["numero"] or "",
                "Origem do número": origem if f["numero"] else "",
                "Situação": "Pendente" if f["problema"] else "OK",
                "Pendência": f["problema"] or "",
            })

        ok = [f for f in finais if f["nome"] and f["numero"] and not f["problema"]]
        if not finais:
            situacao, formatado = ST_VAZIO, ""
        else:
            # monta mesmo com pendência: o que está ok sai no padrão, o resto sai como nome
            pedacos = [montar(f) if f in ok else (f["nome"] or "") for f in finais]
            formatado = ", ".join(p for p in pedacos if p)
            if pendencias:
                situacao = ST_PEND
            elif formatado == original:
                situacao = ST_OK
            elif completou:
                situacao = ST_COMPL
            else:
                situacao = ST_CORR
        linhas_contrato.append({
            "Ano": row["Ano"], "Órgão": row["Órgão"],
            "Número do contrato": row["Número do contrato"],
            "Fornecedor": row["Fornecedor"],
            "Fiscais (original)": row["Fiscais (original)"],
            "Fiscais (corrigido)": formatado,
            "Situação": situacao,
            "Pendências": "; ".join(pendencias),
        })
    return pd.DataFrame(linhas_contrato), pd.DataFrame(linhas_fiscal)


if __name__ == "__main__":
    # Pasta das planilhas: a informada; senão a pasta onde está este script (se tiver planilhas);
    # senão a subpasta "upload" de onde o comando foi executado.
    aqui = Path(__file__).resolve().parent
    if len(sys.argv) > 1:
        pasta = Path(sys.argv[1])
    elif any(aqui.glob("fiscais_contratos_*.xlsx")):
        pasta = aqui
    else:
        pasta = Path("upload")
    # Resultado fica FORA da pasta upload, para não ser publicado junto por engano
    saida = Path(sys.argv[2]) if len(sys.argv) > 2 else pasta.resolve().parent / "fiscais_limpos.xlsx"
    contratos, fiscais = processar(ler_planilhas(pasta))
    with pd.ExcelWriter(saida) as w:
        contratos.to_excel(w, sheet_name="Contratos", index=False)
        fiscais.to_excel(w, sheet_name="Fiscais", index=False)
    print(contratos.groupby(["Ano", "Situação"]).size().unstack(fill_value=0).to_string())
    print(f"\nArquivo gerado: {saida}")
