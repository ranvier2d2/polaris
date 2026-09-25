#!/usr/bin/env python3
"""test_cumbre_carrera.py — dos mutaciones concurrentes sobreviven, y un estado borrado no se resiembra.

`os.replace` es atómico para el fichero, no para leer→modificar→guardar. Dos procesos que
tocan campos distintos se pisan y el primero desaparece. Y `ensure` trataba «no hay fichero»
como primera instalación, así que borrar cumbre.json reescribía la semilla vieja.
"""
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="test_cumbre_carrera_")
os.environ["BTP_STATE_DIR"] = os.path.join(_TMP, "state")
os.environ["BTP_REPO"] = _TMP
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cumbre  # noqa: E402

cumbre.BRUJULA_MD = os.path.join(_TMP, "BRUJULA-NED.md")

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print("  ✗ %s" % name)


ESCRITOR = r'''
import os, sys
sys.path.insert(0, %(tools)r)
import cumbre
cumbre.BRUJULA_MD = %(brujula)r
cumbre.set_saliente(sys.argv[1], siguiente_accion=sys.argv[2])
'''


def _accion(sid):
    d = cumbre.load()
    for s in d["salientes"]:
        if s["id"] == sid:
            return s.get("siguiente_accion")
    return None


def _ronda_procesos(script, env):
    cumbre.set_saliente("biopsia", siguiente_accion="BASE-BIO")
    cumbre.set_saliente("dianas", siguiente_accion="BASE-DIA")
    procs = [
        subprocess.Popen([sys.executable, script, "biopsia", "ACCION-A"], env=env),
        subprocess.Popen([sys.executable, script, "dianas", "ACCION-B"], env=env),
    ]
    err = []
    for p in procs:
        rc = p.wait(timeout=60)
        if rc != 0:
            err.append(rc)
    return _accion("biopsia") == "ACCION-A" and _accion("dianas") == "ACCION-B" and not err


def _hilo(sid, accion, barrera):
    barrera.wait()
    cumbre.set_saliente(sid, siguiente_accion=accion)


def main():
    cumbre.ensure()
    d = cumbre.load()
    check("primera instalación siembra la cadena", os.path.exists(cumbre.CUMBRE) and "NED" in d["meta"])

    script = os.path.join(_TMP, "escritor.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(ESCRITOR % {"tools": os.path.join(ROOT, "tools"), "brujula": cumbre.BRUJULA_MD})
    env = dict(os.environ)
    env["BTP_STATE_DIR"] = os.environ["BTP_STATE_DIR"]
    env["BTP_REPO"] = _TMP
    rondas = [_ronda_procesos(script, env) for _ in range(8)]
    check("dos procesos: las dos mutaciones sobreviven (%d/8)" % sum(rondas), all(rondas))

    cumbre.set_saliente("biopsia", siguiente_accion="BASE-BIO")
    cumbre.set_saliente("dianas", siguiente_accion="BASE-DIA")
    barrera = threading.Barrier(2)
    hilos = [
        threading.Thread(target=_hilo, args=("biopsia", "HILO-A", barrera)),
        threading.Thread(target=_hilo, args=("dianas", "HILO-B", barrera)),
    ]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join(timeout=60)
    check("dos hilos: las dos mutaciones sobreviven",
          _accion("biopsia") == "HILO-A" and _accion("dianas") == "HILO-B")

    cumbre.set_saliente("biopsia", siguiente_accion="MARCA-VIVA")
    os.remove(cumbre.CUMBRE)
    reseembro = False
    try:
        cumbre.ensure()
        reseembro = True
    except ValueError as e:
        check("estado borrado no resiembra la semilla", "semilla" in str(e))
    else:
        check("estado borrado no resiembra la semilla", False)
    check("el json borrado sigue ausente", not os.path.exists(cumbre.CUMBRE) and not reseembro)

    print("RESULTADO cumbre carrera: %d OK, %d fallos" % (_pass, _fail))
    return _fail


if __name__ == "__main__":
    sys.exit(main())
