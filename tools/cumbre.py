#!/usr/bin/env python3
"""tools/cumbre.py — la BRÚJULA del lazo: la ruta a NED como DATO vivo (P1, esquema).

La META es **NED** (sin evidencia de enfermedad). La vacuna personalizada es la **ruta de
hoy** (revisable). Este módulo guarda esa ruta como una cadena ordenada de «salientes»
(el cuello de botella real) con un marcador «aquí estamos» sobre el primer eslabón sin
resolver. El lazo lo lee para elegir QUÉ atacar (el foco), y la **verificación** sube el
marcador (trinquete) SOLO con evidencia — no el modelo a ojo.

Es COORDINACIÓN / ESTADO («dónde estamos, cuál es el siguiente bloqueo»), **NO consejo
clínico ni claims**: el clínico verificado vive en la fuente de verdad; aquí solo se
referencia (campo `fuente`). **Deciden sus médicos.** Sin dependencias (stdlib).
"""
import contextlib
import fcntl
import functools
import json
import os
import sys
import threading
from datetime import datetime

# El estado VIVO (cumbre.json) y la brújula que lee {{TITULAR}} (BRUJULA-NED.md) viven SOLO en casa
# base: tanto tools/state como 00_FUENTE-DE-VERDAD/ están gitignored → NO viajan al worktree.
# Resolvemos casa base (BTP_REPO o ~/claudecode), nunca el árbol relativo al fichero: una sesión
# en su worktree (o `verificacion` con avanzar()) que mueva la cadena clínica debe escribir en la
# ÚNICA brújula viva, no en una copia desechable. Mismo criterio que seguimiento.py/leer_contacto.py
# (commit 2fcfad93). BTP_STATE_DIR sigue aislando el estado en tests; BTP_REPO permite override del raíz.
REPO = os.environ.get("BTP_REPO") or os.path.expanduser("~/claudecode")
STATE = os.environ.get("BTP_STATE_DIR") or os.path.join(REPO, "tools", "state")
CUMBRE = os.path.join(STATE, "cumbre.json")
BRUJULA_MD = os.path.join(REPO, "00_FUENTE-DE-VERDAD", "Gestion", "BRUJULA-NED.md")

ESTADOS = ("pendiente", "en_curso", "hecho", "resuelto", "riesgo", "fallido", "aparcado")
VETOS = ("pendiente", "mejor", "descartada")

# Estados que CIERRAN un eslabón: la cadena ya no se detiene en él. UNA sola definición,
# a nivel de módulo, porque la usan tres sitios que antes divergían (foco(), avanzar() y
# seguimiento.hilos_clinicos()) — esa divergencia era el bug: avanzar() solo excluía
# ('resuelto','aparcado'), así que con la cadena viva (biopsia=hecho) el marcador RETROCEDÍA
# al primer eslabón ya cerrado. `fallido` entra a propósito: un eslabón roto no debe ser un
# tapón que clave la brújula, pero se marca ⚠️ para que tampoco pase en silencio.
CERRADOS = ("resuelto", "hecho", "aparcado", "fallido")
# Estados que puede fijar avanzar(): «hecho» = el acto físico ocurrió (la biopsia se hizo);
# «resuelto» = el eslabón dejó de bloquear. Los dos exigen evidencia, por la misma puerta.
AVANZABLES = ("resuelto", "hecho")

# Seed HONESTO, anclado en `reference-clinical-profile` (verificado). Coordinación, no
# consejo: cada saliente cita su fuente; el detalle clínico vive en la fuente de verdad.
SEED = {
    "meta": "NED — sin evidencia de enfermedad (estar y seguir)",
    # «la ruta que perseguimos», NO «la mejor»: es la decisión/estrella polar de {{TITULAR}}
    # (coordinación), no una conclusión clínica — el comité rankea la vacuna detrás de
    # otras opciones por mérito clínico (reference-clinical-profile). Deciden sus médicos.
    "ruta_actual": "vacuna personalizada (la ruta que perseguimos hacia NED hoy; revisable)",
    "aqui_estamos": "biopsia",
    "salientes": [
        {"id": "biopsia", "titulo": "Re-biopsia de L1 (Zúrich, Dr. {{CONTACTO}} {{CONTACTO}})",
         "estado": "en_curso",
         "bloqueo": "cita por confirmar (depósito 2700 CHF pagado 4-jun); que los cores "
                    "recojan TODO lo de neoantígenos (13-core val. {{CENTRO}}, HLA-LOH, "
                    "PBMC/leukapheresis, RNA-seq, immunopeptidomics, NE IHC, TP53/CCNE1/RB1)",
         "siguiente_accion": "confirmar fecha; checklist molecular a {{CONTACTO}} / {{CENTRO}}",
         "fuente": "reference-clinical-profile; _PRIVADO_CLINICO/{{CARPETA_PRUEBA}}"},
        {"id": "dianas", "titulo": "Identificar dianas / neoantígenos",
         "estado": "pendiente",
         "bloqueo": "depende de la biopsia; pipeline {{CONTACTO}} {{CONTACTO}} ({{CENTRO}}) + "
                    "Lausanne/Bassani-Sternberg. Lever: {{CONTACTO}} dispuesta a venir a Zúrich",
         "siguiente_accion": "(diferido a señal de {{TITULAR}} / confirmación de biopsia)",
         "fuente": "project-contacto-contacto-{{CIUDAD}}; Plan-Predictivo-Neoantigenos-2026-06-21"},
        {"id": "ensayo", "titulo": "Puerta de ensayo / fabricante de la vacuna",
         "estado": "pendiente",
         "bloqueo": "gate = enfermedad medible. Vacuna #1: PNV21 (NCT05098210, Fred Hutch). "
                    "Puertas que saltan el muro de medible: Moffitt NCT06691035, STEMVAC NCT07112053",
         "siguiente_accion": "mapear criterios reales vs su estado por puerta",
         "fuente": "reference-clinical-profile (Mapa-Ensayos-Verificado)"},
        {"id": "acceso", "titulo": "Acceso (viaje / self-pay / legal transfronterizo / fondos)",
         "estado": "pendiente",
         "bloqueo": "US self-pay; MTA y consentimiento US↔Suiza; financiación; logística de muestra",
         "siguiente_accion": "(motor de acceso, con OK de {{TITULAR}})",
         "fuente": "project-contacto-contacto-{{CIUDAD}}; agente legal-burocracia"},
    ],
    "transversal": [
        {"id": "medible", "titulo": "Enfermedad medible (RECIST) — gate de casi todo",
         "estado": "riesgo",
         "bloqueo": "enfermedad ósea-blástica = no medible; D11 irradiado (fuera de tabla). "
                    "Vía: lesión no irradiada o pivotar a ensayos no-medible / bypass",
         "siguiente_accion": "preguntar a coordinadores qué aceptan (no-medible/bone-evaluable/ctDNA)",
         "fuente": "reference-clinical-profile (Lesion-Medible-Informes-2026-06-18)"},
    ],
    "rutas_candidatas": [],
}


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# Claves sin las que esto YA NO es la cadena a NED, sino un fichero cualquiera. Nacieron de un
# incidente real (20-sep-2026): `cumbre.json` apareció con 27 bytes —solo `{"aqui_estamos":
# "biopsia"}`—, sin meta, sin ruta y sin un solo saliente. La cadena se recuperó entera desde
# `BRUJULA-NED.md`, que se había renderizado 9 minutos antes, pero durante horas `cumbre.py`
# reventó con un `KeyError: 'meta'` ilegible, el digest perdió el «AQUÍ ESTAMOS» y el vigía
# priorizó por impacto-NED sin su referencia. Nadie se enteró hasta que alguien miró.
OBLIGATORIAS = ("meta", "ruta_actual", "salientes")


def _valida(payload):
    """Fail-closed: lo que no es la cadena entera NO se escribe. Devuelve el payload o lanza."""
    if not isinstance(payload, dict):
        raise ValueError("cumbre: el estado tiene que ser un dict, no %s" % type(payload).__name__)
    faltan = [k for k in OBLIGATORIAS if not payload.get(k)]
    if faltan:
        raise ValueError(
            "cumbre: me niego a escribir un estado sin %s — eso deja la cadena a NED mutilada. "
            "Si el fichero ya está roto, se reconstruye desde 00_FUENTE-DE-VERDAD/Gestion/"
            "BRUJULA-NED.md, que es su render fiel." % ", ".join(faltan))
    return payload


def _marcar_instalada(path):
    """Deja constancia de que esta ruta ya tuvo cadena. Sobrevive a borrar el json."""
    marca = path + ".instalada"
    if os.path.exists(marca):
        return
    tmp = "%s.tmp.%d" % (marca, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("instalada\n")
    os.replace(tmp, marca)


def _write_atomic(path, payload):
    _valida(payload)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # tmp por PID: con varias sesiones a la vez (aquí, lo normal) un tmp de nombre fijo es una
    # carrera — la misma que tuvo `kb.py` 56 días. Mismo arreglo.
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    _marcar_instalada(path)


# flock entre procesos y RLock entre hilos. La profundidad evita el auto-bloqueo: load()
# llama a ensure(), y avanzar() llama a foco() que vuelve a load(), dentro del mismo escritor.
_CUMBRE_LOCK_DEPTH = 0
_cumbre_hilo = threading.RLock()


@contextlib.contextmanager
def _cumbre_lock():
    global _CUMBRE_LOCK_DEPTH
    _cumbre_hilo.acquire()
    try:
        if _CUMBRE_LOCK_DEPTH > 0:
            _CUMBRE_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _CUMBRE_LOCK_DEPTH -= 1
            return
        os.makedirs(os.path.dirname(CUMBRE), exist_ok=True)
        fd = open(CUMBRE + ".lock", "a+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            _CUMBRE_LOCK_DEPTH = 1
            try:
                yield
            finally:
                _CUMBRE_LOCK_DEPTH = 0
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            finally:
                fd.close()
    finally:
        _cumbre_hilo.release()


def _serializado(fn):
    @functools.wraps(fn)
    def _w(*a, **kw):
        with _cumbre_lock():
            return fn(*a, **kw)
    return _w


def ensure():
    if os.path.exists(CUMBRE):
        return
    # El json puede faltar porque nadie instaló todavía, o porque el estado se borró.
    # La marca la escribe _write_atomic la primera vez que la cadena existe. Si queda
    # la marca y no el json, resembrar SEED taparía la cadena viva con la semilla vieja.
    if os.path.exists(CUMBRE + ".instalada"):
        raise ValueError(
            "cumbre: %s desapareció tras una instalación. No resiembro la semilla "
            "(sería la cadena vieja, no el estado que había). Restaura desde "
            "00_FUENTE-DE-VERDAD/Gestion/BRUJULA-NED.md." % CUMBRE)
    d = dict(SEED)
    d["actualizado"] = _now()
    _write_atomic(CUMBRE, d)


def load():
    """Devuelve la cadena. Si el fichero existe pero está MUTILADO, lo dice en claro.

    Antes esto devolvía el dict tal cual y el fallo salía 200 líneas después como
    `KeyError: 'meta'`, que no dice ni qué pasó ni qué hacer.
    """
    ensure()
    with open(CUMBRE, encoding="utf-8") as f:
        d = json.load(f)
    faltan = [k for k in OBLIGATORIAS if not d.get(k)]
    if faltan:
        raise ValueError(
            "cumbre: %s está mutilado, le faltan %s. La cadena a NED se reconstruye desde "
            "00_FUENTE-DE-VERDAD/Gestion/BRUJULA-NED.md, que es su render fiel."
            % (CUMBRE, ", ".join(faltan)))
    return d


def _save(d):
    d["actualizado"] = _now()
    _write_atomic(CUMBRE, d)
    try:
        render_brujula(d)
    except Exception as e:
        sys.stderr.write("cumbre: aviso, no pude renderizar BRUJULA-NED.md (%r)\n" % e)


def foco():
    """El saliente actual («aquí estamos») = el siguiente cuello de botella a atacar.

    Blindaje, con el MISMO criterio en las dos ramas (antes divergían y esa era la raíz del
    retroceso): si `aqui_estamos` cuelga (id inexistente) o apunta a un eslabón YA CERRADO
    (p.ej. una edición manual dejó el marcador en la biopsia hecha), NO se devuelve eso en
    silencio —la brújula apuntaría hacia atrás—: se degrada al primer saliente accionable.
    """
    d = load()
    aqui = d.get("aqui_estamos")
    salientes = d.get("salientes", [])
    for s in salientes:
        if s["id"] == aqui:
            if s.get("estado") in CERRADOS:
                sys.stderr.write(
                    "cumbre: aviso, aqui_estamos=%r está %s (cerrado) "
                    "→ uso el primer nodo accionable\n" % (aqui, s.get("estado")))
                break
            return s
    else:
        if aqui:
            sys.stderr.write(
                "cumbre: aviso, aqui_estamos=%r no existe entre los salientes "
                "→ uso el primer nodo accionable\n" % (aqui,))
    return next((s for s in salientes if s.get("estado") not in CERRADOS), None)


def set_saliente(sid, **campos):
    """Actualiza campos de un saliente (estado/bloqueo/siguiente_accion/fuente/titulo)."""
    d = load()
    if "estado" in campos and campos["estado"] not in ESTADOS:
        raise ValueError("estado invalido: %r" % campos["estado"])
    if campos.get("estado") in CERRADOS:
        # Cerrar un eslabón SOLO por avanzar()/aparcar(), que exigen evidencia o motivo. Si no,
        # el trinquete se puentearía: marcarlo cerrado sin justificar y dejar que avanzar()
        # salte el nodo. Antes solo se vetaba «resuelto», así que «hecho»/«aparcado»/«fallido»
        # eran una puerta trasera al mismo salto del marcador.
        raise ValueError(
            "trinquete: '%s' cierra el eslabón — usa avanzar() con evidencia "
            "(resuelto/hecho) o aparcar() con motivo, no set_saliente" % campos["estado"])
    for grupo in ("salientes", "transversal"):
        for s in d.get(grupo, []):
            if s["id"] == sid:
                s.update({k: v for k, v in campos.items()
                          if k in ("estado", "bloqueo", "siguiente_accion", "fuente", "titulo")})
                _save(d)
                return True
    return False


def avanzar(sid, evidencia, estado="resuelto"):
    """Trinquete: cierra un saliente y mueve «aquí estamos» al siguiente ABIERTO. EXIGE
    evidencia (lo invoca `verificacion`, no el modelo a ojo). Devuelve el nuevo foco o None.

    `estado` distingue lo que la cadena viva ya expresaba a mano: «hecho» = el acto físico
    ocurrió (la biopsia se hizo, faltan los resultados); «resuelto» = el eslabón dejó de
    bloquear. Misma puerta, misma exigencia de evidencia.
    """
    if not evidencia or not str(evidencia).strip():
        raise ValueError("avanzar exige evidencia verificada (trinquete A5)")
    if estado not in AVANZABLES:
        raise ValueError("avanzar solo cierra con %s (recibido %r)" % ("/".join(AVANZABLES), estado))
    d = load()
    cadena = d.get("salientes", [])
    if not any(s.get("id") == sid for s in cadena):
        raise ValueError("saliente inexistente: %r" % sid)
    for s in cadena:
        if s["id"] == sid:
            s["estado"] = estado
            s["evidencia"] = str(evidencia).strip()[:500]
            s["resuelto_en"] = _now()
    # nuevo «aquí estamos» = primer saliente ABIERTO. Usa CERRADOS (la definición única): con
    # ('resuelto','aparcado') a secas el marcador retrocedía al primer eslabón «hecho».
    nuevo = next((s["id"] for s in cadena if s.get("estado") not in CERRADOS), None)
    d["aqui_estamos"] = nuevo
    _marcar_espera(cadena, nuevo)
    _save(d)
    return foco()


def _marcar_espera(cadena, sid):
    """Sella desde cuándo el eslabón que pasa a ser el foco está esperando.

    Sin esto la cadena clínica NO ENVEJECE: los nodos no tienen ningún campo de fecha, así que
    «esperando resultados de la biopsia» pesa y se lee exactamente igual el día 1 que el día 17.
    Con la fecha, el parte puede decir cuántos días llevas, que es lo que convierte una línea
    informativa en «toca follow-up».
    """
    if not sid:
        return
    hoy = datetime.now().strftime("%Y-%m-%d")
    for s in cadena:
        if s.get("id") == sid and not s.get("esperando_desde"):
            s["esperando_desde"] = hoy


def esperando_desde(sid, desde=None):
    """Fija a mano desde cuándo se espera en un eslabón (para la cadena que ya venía viva sin
    fecha). `desde` en ISO YYYY-MM-DD; por defecto hoy."""
    fecha = str(desde or datetime.now().strftime("%Y-%m-%d"))[:10]
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise ValueError("fecha ISO YYYY-MM-DD, no %r" % desde)
    d = load()
    for grupo in ("salientes", "transversal"):
        for s in d.get(grupo, []):
            if s.get("id") == sid:
                s["esperando_desde"] = fecha
                _save(d)
                return True
    return False


def aparcar(sid, motivo):
    """Cierra un eslabón SIN resolverlo (pausa deliberada). Exige motivo, por la misma razón
    que avanzar() exige evidencia: aparcar mueve el marcador igual que resolver."""
    if not motivo or not str(motivo).strip():
        raise ValueError("aparcar exige motivo (aparcar mueve el marcador igual que resolver)")
    d = load()
    cadena = d.get("salientes", [])
    if not any(s.get("id") == sid for s in cadena):
        raise ValueError("saliente inexistente: %r" % sid)
    for s in cadena:
        if s["id"] == sid:
            s["estado"] = "aparcado"
            s["motivo_aparcado"] = str(motivo).strip()[:500]
            s["aparcado_en"] = _now()
    nuevo = next((s["id"] for s in cadena if s.get("estado") not in CERRADOS), None)
    d["aqui_estamos"] = nuevo
    _marcar_espera(cadena, nuevo)
    _save(d)
    return foco()


def add_ruta_candidata(titulo, *, fuente="", veto="pendiente", nota=""):
    """Radar de rutas: registra una ruta candidata a NED. Solo se re-apunta si se VETA
    como 'mejor' (la decisión es de {{TITULAR}} + comité; aquí solo se registra)."""
    if veto not in VETOS:
        raise ValueError("veto invalido: %r" % veto)
    d = load()
    d.setdefault("rutas_candidatas", []).append(
        {"titulo": str(titulo), "fuente": str(fuente), "veto": veto,
         "nota": str(nota), "registrada": _now()})
    _save(d)
    return True


ensure = _serializado(ensure)
set_saliente = _serializado(set_saliente)
avanzar = _serializado(avanzar)
esperando_desde = _serializado(esperando_desde)
aparcar = _serializado(aparcar)
add_ruta_candidata = _serializado(add_ruta_candidata)


def estado():
    """Resumen legible (también se vuelca a BRUJULA-NED.md en _save/render)."""
    d = load()
    f = foco()
    lineas = ["Meta: %s" % d["meta"], "Ruta de hoy: %s" % d["ruta_actual"],
              "AQUÍ ESTAMOS → %s (%s)" % (f["titulo"], f["estado"]) if f else "AQUÍ ESTAMOS → (sin foco)"]
    for s in d.get("salientes", []):
        marca = "⮕" if s["id"] == d.get("aqui_estamos") else " "
        # `fallido` cierra la cadena (no la tapona) pero NO puede pasar en silencio.
        alerta = " ⚠️ FALLIDO" if s.get("estado") == "fallido" else ""
        lineas.append("  %s [%s] %s%s" % (marca, s["estado"], s["titulo"], alerta))
    render_brujula(d)
    return "\n".join(lineas)


def render_brujula(d=None):
    """Vuelca la brújula a Gestion/BRUJULA-NED.md (legible para {{TITULAR}})."""
    d = d or load()
    os.makedirs(os.path.dirname(BRUJULA_MD), exist_ok=True)
    out = ["# 🧭 Brújula a NED", "",
           "> Coordinación / estado — **NO consejo clínico**; deciden sus médicos. "
           "El clínico verificado vive en la fuente de verdad; aquí solo se referencia.",
           "", "**Meta (cumbre):** %s" % d["meta"],
           "**Ruta de hoy:** %s" % d["ruta_actual"],
           "**Actualizado:** %s" % d.get("actualizado", "?"), "",
           "## Cadena de salientes (el cuello de botella real)"]
    for s in d.get("salientes", []):
        aqui = " ⬅️ **AQUÍ ESTAMOS**" if s["id"] == d.get("aqui_estamos") else ""
        alerta = " ⚠️ **FALLIDO**" if s.get("estado") == "fallido" else ""
        out.append("\n### [%s] %s%s%s" % (s["estado"], s["titulo"], alerta, aqui))
        out.append("- Bloqueo: %s" % s.get("bloqueo", "—"))
        out.append("- Siguiente: %s" % s.get("siguiente_accion", "—"))
        out.append("- Fuente: %s" % s.get("fuente", "—"))
        if s.get("evidencia"):
            verbo = "Hecho" if s.get("estado") == "hecho" else "Resuelto"
            out.append("- ✅ %s (%s): %s" % (verbo, s.get("resuelto_en", ""), s["evidencia"]))
        if s.get("motivo_aparcado"):
            out.append("- ⏸️ Aparcado (%s): %s" % (s.get("aparcado_en", ""), s["motivo_aparcado"]))
    out.append("\n## Transversal (gates que afectan a todo)")
    for s in d.get("transversal", []):
        out.append("- [%s] **%s** — %s (%s)" % (s["estado"], s["titulo"], s.get("bloqueo", ""), s.get("fuente", "")))
    out.append("\n## Radar de rutas (¿hay un camino mejor a NED?)")
    rc = d.get("rutas_candidatas", [])
    if not rc:
        out.append("- (sin candidatas; commit a la ruta de hoy. Re-apuntar SOLO si una se veta como 'mejor'.)")
    for r in rc:
        out.append("- [veto:%s] %s — %s %s" % (r.get("veto"), r.get("titulo"), r.get("nota", ""), "(" + r["fuente"] + ")" if r.get("fuente") else ""))
    fd = os.open(BRUJULA_MD, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(out) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _arg(a, name, default=None):
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def main(argv):
    cmd = argv[0] if argv else "estado"
    a = argv[1:]
    if cmd == "seed":
        ensure()
        render_brujula()
        print("cumbre.json sembrada en %s" % CUMBRE)
        return 0
    if cmd == "foco":
        f = foco()
        print(json.dumps(f, ensure_ascii=False) if f else "(sin foco)")
        return 0
    if cmd == "estado":
        print(estado())
        return 0
    if cmd == "show":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "set":
        sid = _arg(a, "--id")
        campos = {k.lstrip("-"): _arg(a, k) for k in ("--estado", "--bloqueo", "--siguiente_accion", "--fuente", "--titulo") if _arg(a, k) is not None}
        print("ok" if sid and set_saliente(sid, **campos) else "no encontrado")
        return 0
    if cmd == "avanzar":
        sid = _arg(a, "--id")
        ev = _arg(a, "--evidencia", "")
        est = _arg(a, "--estado", "resuelto")
        try:
            nf = avanzar(sid, ev, estado=est)
            print("nuevo foco:", nf["titulo"] if nf else "(cadena resuelta)")
            return 0
        except ValueError as e:
            print("error:", e)
            return 1
    if cmd == "esperando":
        sid = _arg(a, "--id")
        try:
            print("ok" if sid and esperando_desde(sid, _arg(a, "--desde")) else "no encontrado")
            return 0
        except ValueError as e:
            print("error:", e)
            return 1
    if cmd == "aparcar":
        sid = _arg(a, "--id")
        try:
            nf = aparcar(sid, _arg(a, "--motivo", ""))
            print("nuevo foco:", nf["titulo"] if nf else "(cadena cerrada)")
            return 0
        except ValueError as e:
            print("error:", e)
            return 1
    if cmd == "ruta":
        add_ruta_candidata(_arg(a, "--titulo", ""), fuente=_arg(a, "--fuente", ""),
                           veto=_arg(a, "--veto", "pendiente"), nota=_arg(a, "--nota", ""))
        print("ruta candidata registrada")
        return 0
    print("uso: cumbre.py [seed|foco|estado|show|set --id X [--estado..]|"
          "avanzar --id X --evidencia '..' [--estado resuelto|hecho]|"
          "aparcar --id X --motivo '..'|ruta --titulo '..']")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
