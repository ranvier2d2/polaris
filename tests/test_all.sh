#!/bin/bash
# test_all.sh — corre toda la batería del sistema (muro + lazo P1) en verde o falla.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/usr/bin/python3; [ -x "$PY" ] || PY=python3
fail=0
skip=0
# Los logs de los rojos, en una carpeta POR EJECUCIÓN (25-sep-26, deuda
# test-all-log-rojo-tmp-compartido): con /tmp/rojo-<test>.log fijo, las sesiones en paralelo se
# pisaban el log y el de una rama enseñaba el fallo de otra. En el CI hay un runner por ejecución
# y el workflow público y healthcheck los buscan en /tmp: allí se queda. Test: test_all_rojo_dir.py.
if [ -n "$BTP_ROJO_DIR" ]; then ROJO_DIR="$BTP_ROJO_DIR"; mkdir -p "$ROJO_DIR"
elif [ -n "$CI" ]; then ROJO_DIR=/tmp
else _t="${TMPDIR:-/tmp}"; ROJO_DIR=$(mktemp -d "${_t%/}/rojo.XXXXXX"); fi
ROJO_DIR="${ROJO_DIR%/}"
# 19-sep-2026 — BTP_PORTABLE=1 (lo usa el CI del repo público en Linux): salta las baterías
# que solo pueden pasar en la casa base: Llavero de macOS, plists de launchd, el binario
# `xurl`, el panel técnico de la anatomía. No son opcionales, es que allí no hay con qué
# correrlas. La batería completa sigue siendo la de casa base.
# 20-sep-2026: entra `test_healthcheck_halt_inactividad.py`. Sus 3 comprobaciones del roster
# llaman a `hc._check_roster_daemons()`, que sin `launchctl` no puede listar nada: en Linux
# daban 10 OK y 3 fallos FIJOS, y un rojo permanente convierte el CI en decoración. Las otras
# 10 sí pasarían allí; se pierden a cambio de que el semáforo vuelva a significar algo.
SOLO_CASA_BASE="test_xurl.py test_x_guardados_enriquecido.py test_llavero_mudo.py
test_bucles_colgados.py test_plists_home.py test_anatomia_tecnica.py test_auto_mejora_turnos.py
test_digest.sh test_muro_costura_rm.py test_coste_repo.py test_healthcheck_halt_inactividad.py"
# `$(echo …)` colapsa los saltos de línea de la lista: sin eso, las baterías que caen al
# principio o al final de cada línea no casaban y seguían corriendo (4 rojos en el primer CI).
_salta() { [ -n "$BTP_PORTABLE" ] || return 1
           case " $(echo $SOLO_CASA_BASE) " in *" $1 "*) return 0;; esac; return 1; }
run() { _salta "$1" && { echo "── $1 ── (solo casa base)"; skip=$((skip+1)); return 0; }; echo "── $1 ──"; bash "$ROOT/tests/$1" >/tmp/t.$$ 2>&1; local rc=$?; tail -1 /tmp/t.$$;
        [ $rc -eq 77 ] && { skip=$((skip+1)); return 0; }
        [ $rc -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-$1.log" 2>/dev/null;
                           echo "  🔴 ROJO: $1 (rc=$rc · log: $ROJO_DIR/rojo-$1.log)"; }; }
# El nombre del test que se pone ROJO se DICE (27/7/26). Antes runpy solo incrementaba el contador:
# la batería acababa en "❌ 1 batería(s) con fallos" sin decir cuál, y había que ir a mano fichero a
# fichero. Con el log guardado, además, el fallo se puede mirar después (importa para los flakes).
runpy() { _salta "$1" && { echo "── $1 ── (solo casa base)"; skip=$((skip+1)); return 0; }; echo "── $1 ──"; "$PY" "$ROOT/tests/$1" >/tmp/t.$$ 2>/tmp/t.$$.err; local rc=$?; tail -1 /tmp/t.$$;
          [ $rc -eq 77 ] && { skip=$((skip+1)); return 0; }
          # stderr va al log del rojo (22-sep-26): `unittest` escribe AHÍ el fallo, y sin esto el
          # paso «Qué falló exactamente» del CI público salía vacío con test_web_lint en rojo.
          [ $rc -ne 0 ] && { fail=$((fail+1)); cat /tmp/t.$$ /tmp/t.$$.err > "$ROJO_DIR/rojo-$1.log" 2>/dev/null;
                             echo "  🔴 ROJO: $1 (rc=$rc · log: $ROJO_DIR/rojo-$1.log)"; }; }

run   test_fuga.sh
run   test_halt.sh
runpy test_muro_fase0.py
runpy test_muro_clase_plantar.py
runpy test_token_rotacion.py
runpy test_terminos_carril.py
runpy test_f1_opus_supervision.py
runpy test_cola.py
runpy test_cola_diario.py
runpy test_entorno_cola_aislada.py
runpy test_tools_no_sombrea_stdlib.py
runpy test_healthcheck_drift_plists.py
runpy test_prueba_entregable.py
runpy test_dispatcher_env_turnos.py
runpy test_enruta.py
runpy test_enruta_comite.py
runpy test_decide_peticion.py
runpy test_lentes.py
runpy test_gate_salida.py
runpy test_replay_gate.py
runpy test_gate_punto08.py
runpy test_nvidia_tope.py
runpy test_readme_modelos.py
runpy test_stdin_canalizado.py
runpy test_bash_return_explicito.py
runpy test_casa_base_guard.py
runpy test_gate_etiqueta.py
runpy test_gate_escalera.py   # 25-sep · escalera del gate con listón numérico (idea de {{CONTACTO}} + KAI)
runpy test_gate_citas.py
runpy test_all_rojo_dir.py   # 25-sep · cada ejecución guarda sus rojos en SU carpeta (deuda test-all-log-rojo-tmp-compartido)
runpy test_gate_red_caida.py   # 25-sep · punto 07 {{CONTACTO}}+KAI: sin red, la cita sale «sin verificar», nunca verificada
runpy test_gate_preclinico.py
runpy test_verifica_citas_estados.py
runpy test_tier_evidencia.py
runpy test_soporte_cita.py
runpy test_soporte_cita_juez.py
# (no publicado: cubre un detector de PHI que vive solo en local)
runpy test_deuda_texto_sin_alarma.py
runpy test_deuda_duplicadas.py
runpy test_rodaje_utc.py
runpy test_rodaje_casa_base.py
runpy test_rodaje_hook_fijo.py
runpy test_replay_hook_roto.py
runpy test_replay_guard_json.py
runpy test_kpi_ned.py
runpy test_backup.py
runpy test_salud_reconciliar.py
runpy test_hoy_ruta_unica.py
runpy test_obs_nombra_el_trabajo.py
runpy test_migrar_secretos.py
runpy test_etiquetar_hilos.py
runpy test_agentes_frontmatter.py
runpy test_modelo_coherente.py
runpy test_agentes_ritmo.py
runpy test_radar_personas.py
runpy test_salida_guard.py
runpy test_salida_guard_vias.py   # 24-sep · auditoría 3.2: lo que lleva datos a la red se juzga por el DESTINO
runpy test_nivel_salida.py   # 25-sep · P3 F1: nivel de cada salida EN SOMBRA (anota, no manda)
runpy test_ok_envio_blindado.py
runpy test_correos_publicables.py
runpy test_entrada_guard.py
runpy test_audit_comites_uso.py
runpy test_cost_guard.py
runpy test_bot_triage.py
runpy test_triage_route_enrutado.py
runpy test_buzon_ideas.py
runpy test_cosecha_correcciones.py
runpy test_repeticiones_semana.py
runpy test_gate_subagente.py
runpy test_subagente_contexto.py
runpy test_cosecha_hilos.py
runpy test_cosecha_entregables.py
runpy test_archivar_nota.py
runpy test_email_archive_reindex.py   # 25-sep · tras archivar correo, el RAG se reindexa en el acto
runpy test_colgados_stdin.py
runpy test_raices_casa_base.py
runpy test_radar_no_silenciar.py
runpy test_deuda_disponibilidad.py
runpy test_deuda_cerrar_ejecuta.py
runpy test_atribucion.py   # 24-sep · regla de {{TITULAR}}: todo lo que alguien aporta, con su nombre y en «Gracias»
runpy test_clave_deuda_una_forma.py   # 24-sep · una sola forma de clave (backticks) + la alerta resumen exige su hallazgo concreto
runpy test_ramas_fusionar.py
runpy test_ocr_layout.py
runpy test_salida_reintento.py
runpy test_salida_idempotente.py   # 24-sep · auditoría 3.5: una aprobación entrega UNA vez; lo incierto no se reenvía solo
runpy test_dominios_con_dueno.py
runpy test_honestidad_lint.py
runpy test_verifica_citas.py
runpy test_verifica_citas_datacite.py   # 24-sep · un 404 de Crossref no es cita fabricada (Zenodo/DataCite) + DOI sin markdown pegado
runpy test_memoria_radar.py
runpy test_memoria_sistema.py
runpy test_constitucion_sin_perdida.py
runpy test_normas_registro.py
runpy test_normas_gracia.py
runpy test_normas_mecanizadas.py
runpy test_githooks_base.py
runpy test_githooks_marcas.py
runpy test_recall_memoria.py
runpy test_perfil_clinico_al_dia.py
runpy test_portero_ruido.py
runpy test_parte_exento_cupo.py
runpy test_aplazados_pendientes.py   # 24-sep · el parte recoge lo aplazado de CUALQUIER día (855 avisos perdidos desde el 27-jul)
runpy test_regla_en_accion.py
runpy test_hooks_ejecutables.py
runpy test_guard_timeout.py   # 25-sep · {{CONTACTO}}/KAI: un guard lento deniega, no deja pasar; deny de respaldo
runpy test_cerrar_sesion_conflicto.py
runpy test_run_agent_casa_master.py
runpy test_ff_al_abrir.py
runpy test_mini.py
runpy test_lazo_estres.py
runpy test_codigo_rojo.py
runpy test_codigo_rojo_repeticion.py
runpy test_decision_alto_riesgo.py
runpy test_fuente_clinica.py
runpy test_biomarcadores_vhio.py
runpy test_biomarcadores_ggt_alias.py
runpy test_biomarcadores_fecha_extraccion.py
runpy test_biomarcadores_muestras.py
runpy test_rag_lab_origen.py  # analíticas activas extraídas del PDF, no transcritas a mano (25-sep)
runpy test_web_citas_futuras.py  # web: ninguna cita futura con día junto a un lugar (acoso, 25-sep)
runpy test_frescura_dosier.py
runpy test_dosier_invariantes.py
runpy test_cotejo_invariante.py
runpy test_elegibilidad_ensayos.py
runpy test_guardian_evals.py
runpy test_cumbre.py
runpy test_cumbre_integridad.py
runpy test_cascada_clinica.py
runpy test_seguimiento.py
runpy test_seguimiento_carrera.py
runpy test_seguimiento_objetivo_ned.py
runpy test_seguimiento_hecho_cuando.py
runpy test_avisos_nueva_tarea.py
runpy test_reconciliar_estado.py
runpy test_dedup_hilos.py
runpy test_esquema_estado.py
runpy test_web_novedad.py
runpy test_web_lint.py
runpy test_caso_publico.py
runpy test_seguridad_sweep_daemon.py
runpy test_seguridad_sweep.py
runpy test_pipeline_vacuna.py
runpy test_pipeline_datos_reales.py
runpy test_pipeline_alelos_muestra.py
runpy test_investigacion_fuga.py
runpy test_vega_gate.py
runpy test_centinela_ned.py
runpy test_radar_ned_diario.py
runpy test_radar_navegador.py
runpy test_persecucion.py
runpy test_memoria_contactos.py
runpy test_anticipa.py
runpy test_vega_metrics.py
# (no publicado: cubre un detector de PHI que vive solo en local)
runpy test_correo_imap.py
# (no publicado: cubre un detector de PHI que vive solo en local)
# (no publicado: cubre un detector de PHI que vive solo en local)
# (no publicado: cubre un detector de PHI que vive solo en local)
runpy test_historial_sync.py
runpy test_healthcheck_drive.py
runpy test_healthcheck_cerebro.py
runpy test_healthcheck_ci_publico.py
runpy test_subir_historial_drive.py
runpy test_subir_historial_ocr_sidecar.py  # el verificador de identidad LEE el OCR (X.pdf.ocr.txt)
runpy test_pendientes.py
runpy test_correo_smtp.py
runpy test_correo_triage.py
runpy test_triage_route_correo.py
# (no publicado: cubre un detector de PHI que vive solo en local)
runpy test_instagram_dm.py
runpy test_dm_inbox_buzones.py
# (no publicado: cubre un detector de PHI que vive solo en local)
runpy test_borde.py
runpy test_canarios.py
runpy test_cerebro_enlace.py
runpy test_reescribe_consulta.py
runpy test_deid.py
runpy test_deid_procedencia.py   # 24-sep · auditoría 3.3: lo del caso es N2 por procedencia aunque el detector no vea nada
runpy test_kb_pdf_avisos.py
runpy test_kb_pdf_truncado.py
runpy test_kb_fts5.py
runpy test_kb_hibrido.py
runpy test_kb_lock.py
runpy test_contexto_caso.py
runpy test_ia.py
runpy test_cn_fetch.py
runpy test_cde_fetch.py
runpy test_cn.py
runpy test_archivar_nota_honesto.py
runpy test_archivar_nota_venv.py
runpy test_umami_pagina.py
runpy test_centralita_cerebros.py
runpy test_cerebros_generativo.py
runpy test_evidencia_no_cuelga.py
runpy test_oauth_refresh.py
runpy test_freno_criticidad.py
runpy test_run_agent_f2.py
runpy test_canario_muro.py
runpy test_claude_lazo.py
runpy test_xurl.py
runpy test_x_mcp_puente.py   # el MCP de X relanza su puente en vez de quedarse ciego (14-sep-26)
runpy test_x_daemons.py
runpy test_x_guardados_enriquecido.py
runpy test_ramas_detached.py
runpy test_poda_no_se_lleva_ignorados.py
runpy test_git_mutex_poda.py
runpy test_autopoda_residuo.py     # 22-sep · los worktrees fusionados se podan solos; el residuo de tests no los bloquea, lo dudoso sí
runpy test_ramas_conflictos.py
runpy test_ramas_borrador.py
runpy test_cierre_continuidad.py
runpy test_traspaso_compact.py
runpy test_bot_free.py
runpy test_bot_heartbeat.py
runpy test_responder_datos.py
runpy test_healthcheck.py
runpy test_llavero_mudo.py
runpy test_saldo_api.py
runpy test_healthcheck_syspath.py
runpy test_jobs_caidos.py
runpy test_healthcheck_acuse.py
runpy test_healthcheck_llms.py
runpy test_perplexity_agent.py
runpy test_healthcheck_alerta_str.py
runpy test_bucles_colgados.py
runpy test_activar_daemon.py
runpy test_activar_daemon_deshabilitado.py
runpy test_plists_home.py
runpy test_correo_cuenta_principal.py
runpy test_vigia.py
runpy test_anatomia.py
runpy test_anatomia_tecnica.py
runpy test_anatomia_mapa.py
runpy test_anatomia_al_dia.py
runpy test_traza_subagente.py
runpy test_ciclo_agentes.py
runpy test_presencia_cc.py
runpy test_anatomia_push.py
runpy test_errores.py
runpy test_rc_turnos_agotados.py
runpy test_auto_mejora_turnos.py
runpy test_cola_turnos.py
runpy test_evals.py
runpy test_borde_gateway.py
runpy test_yt_inbox.py
runpy test_mcp_server.py
run   test_git_barrido_poda.sh  # 22-sep · la poda diaria no depende de que el modelo esté disponible
run   test_dispatcher.sh
runpy test_panel_aislado.py       # 22-sep · la batería no escribe en el PANEL-LAZO de verdad (bloqueaba la poda)
run   test_credito_agotado.sh

# Mutantes sobre los frenos del MURO: una defensa sin mutante que la mate no está cubierta.
# ~20 s. Nació de dos checks que pasaban EN VACÍO el 20-sep-26 y que solo aparecieron al
# romper a propósito lo que decían proteger.
echo "── mutantes: tests/mutantes/lector_clinico.json ──"
"$PY" "$ROOT/tools/mutantes.py" tests/mutantes/lector_clinico.json >/tmp/t.$$ 2>&1; _rcm=$?; tail -1 /tmp/t.$$
[ $_rcm -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-mutantes.log" 2>/dev/null;
                     echo "  🔴 ROJO: campaña de mutantes (log: $ROJO_DIR/rojo-mutantes.log)"; }
echo "── mutantes: tests/mutantes/soporte_cita.json ──"
"$PY" "$ROOT/tools/mutantes.py" tests/mutantes/soporte_cita.json >/tmp/t.$$ 2>&1; _rcm=$?; tail -1 /tmp/t.$$
[ $_rcm -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-mutantes-soporte.log" 2>/dev/null;
                     echo "  🔴 ROJO: campaña de mutantes soporte_cita (log: $ROJO_DIR/rojo-mutantes-soporte.log)"; }
echo "── mutantes: tests/mutantes/biomarcadores.json ──"
"$PY" "$ROOT/tools/mutantes.py" tests/mutantes/biomarcadores.json >/tmp/t.$$ 2>&1; _rcm=$?; tail -1 /tmp/t.$$
[ $_rcm -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-mutantes-biomarcadores.log" 2>/dev/null;
                     echo "  🔴 ROJO: campaña de mutantes biomarcadores (log: $ROJO_DIR/rojo-mutantes-biomarcadores.log)"; }
echo "── mutantes: tests/mutantes/biomarcadores_fecha.json ──"
"$PY" "$ROOT/tools/mutantes.py" tests/mutantes/biomarcadores_fecha.json >/tmp/t.$$ 2>&1; _rcm=$?; tail -1 /tmp/t.$$
[ $_rcm -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-mutantes-biomarcadores-fecha.log" 2>/dev/null;
                     echo "  🔴 ROJO: campaña de mutantes biomarcadores_fecha (log: $ROJO_DIR/rojo-mutantes-biomarcadores-fecha.log)"; }
echo "── mutantes: tests/mutantes/caso_publico.json ──"
"$PY" "$ROOT/tools/mutantes.py" tests/mutantes/caso_publico.json >/tmp/t.$$ 2>&1; _rcm=$?; tail -1 /tmp/t.$$
[ $_rcm -ne 0 ] && { fail=$((fail+1)); cp /tmp/t.$$ "$ROJO_DIR/rojo-mutantes-caso-publico.log" 2>/dev/null;
                     echo "  🔴 ROJO: campaña de mutantes caso_publico (log: $ROJO_DIR/rojo-mutantes-caso-publico.log)"; }

# Pieza 10 del arnés agéntico: drift de agentes críticos (determinista, sin LLM)
echo "── evals/test_drift_agentes.py ──"
"$PY" "$ROOT/evals/test_drift_agentes.py" >/tmp/t.$$ 2>/dev/null; _rc=$?; tail -1 /tmp/t.$$; [ $_rc -ne 0 ] && fail=$((fail+1))

# Sistema de errores — Fases 2, 3a, 3c
runpy test_recover.py
runpy test_rotar_logs.py
runpy test_viajes_precios.py

# ── Huérfanos enganchados el 25-jul-26 (auditoría) ───────────────────────────
# Estaban escritos y en verde pero NADIE los corría: cada test nuevo había que añadirlo
# a mano aquí y se olvidaba. Ese desfase es lo que dejó invisible durante un MES que
# test_audit_constelacion estaba en rojo y que el digest de guardados estaba muerto.
# Cuatro de ellos son baterías del MURO: «TODO EN VERDE» lo nombraba sin haberlas corrido.
run   test_digest.sh
runpy test_muro_hook.py
runpy test_muro_base_gate.py
runpy test_muro_costura_rm.py
runpy test_muro_a1_sandbox_escalada.py
runpy test_muro_secreto_stdout.py
runpy test_clinico_guard.py
runpy test_log_auditoria_casa_base.py
runpy test_lector_clinico_binario.py
runpy test_mutantes.py
runpy test_visor3d.py
runpy test_visor3d_mps.py
runpy test_esqueleto_niveles.py
runpy test_secretos_largos.py
runpy test_secretos_largos_paralelo.py
runpy test_guarda_memoria.py
runpy test_visor3d_cuelgue.py
runpy test_visor3d_malla_recorte.py
runpy test_visor3d_ficha.py
runpy test_visor3d_carga.py
runpy test_visor3d_marcas.py
runpy test_visor3d_cateter.py
runpy test_visor3d_losa.py
runpy test_visor3d_procedencia.py
runpy test_visor3d_colab.py
runpy test_sonda_silencio.py
runpy test_vigia_latidos.py
runpy test_cola_ruido.py
runpy test_worktree_guard.py
runpy test_zonas_clinicas.py
runpy test_drive_gate.py
runpy test_audit_constelacion.py
runpy test_audit_herramientas.py
runpy test_x_guardados_honestidad.py
runpy test_x_guardados_cli.py
runpy test_onco.py
runpy test_inventario.py
runpy test_inventario_viejas_modelos.py   # 24-sep · issues #2 y #5 (PR #23 de fuera, incorporado con cambios)
runpy test_estado_rutina.py               # 24-sep · issue #4 (PR #22 de fuera, incorporado con cambios)
runpy test_rutinas_latido.py              # 25-sep · cadencia real + periodo declarado + cola del comité ({{CONTACTO}}+KAI)
runpy test_audit_agentes_daemon.py
runpy test_publicar_fuga.py
runpy test_publicar_personas.py  # ficha de persona `alto`: ni su apellido ni su ficha salen al espejo
runpy test_publicar_publicos.py  # excepción con consentimiento: la cadena exacta, solo en sus ficheros
runpy test_espejo_reiniciar_historial.py  # reiniciar el historial del espejo: ensayo por defecto, sin la palabra no empuja
runpy test_publicar_overlay_casa_base.py
runpy test_publicar_sync.py
runpy test_publicar_sync_candado.py  # dos publicaciones a la vez NO se pisan el árbol
runpy test_lock_dueno.py          # 25-sep · el candado compartido no se le quita a un dueño VIVO
runpy test_espejo_ensayo.py  # no se publica en rojo, y el espejo se reconoce por su marca
runpy test_ci_barrido.py
runpy test_capacidades.py
runpy test_centralita_blacklist.py
runpy test_correo_smtp_gate.py
runpy test_cosecha_checklists.py
runpy test_cronica.py
runpy test_elicit.py
runpy test_git_mutex.py
runpy test_git_mutex_freno_base.py
runpy test_git_mutex_merge_fallido.py
runpy test_singleton_guard.py
runpy test_rama_vista_guard.py
runpy test_copy_web_guard.py
runpy test_reservas_decision.py
runpy test_identidad_paciente.py
runpy test_identidad_tabla.py       # 24-sep · la ventanilla marcaba como AJENAS sus propias Rx de 2024 (tabla markdown)
runpy test_borrador_unico.py
runpy test_web_i18n.py
runpy test_paso_consolidacion.py
runpy test_caja.py
runpy test_cosecha_panel.py
runpy test_contrato_asiento.py
runpy test_portguard.py
runpy test_puertos_loopback.py
runpy test_observatorio_movil.py
runpy test_rebuild_agents.py

# ── Hallazgos de impacto MEDIO de la auditoría del 25-jul-26 ─────────────────────
# Cada uno cierra un hallazgo con ficha del informe. La regla es que «arreglado» solo
# existe con un TEST: sin esto, el arreglo es una afirmación.
runpy test_reap_vivo.py             # nº7   · reap_stuck no rescata lo que sigue corriendo
runpy test_healthcheck_deadman.py   # nº4+5 · el acuse encola de verdad · dead-man sin `pending>0`
runpy test_healthcheck_halt_inactividad.py  # el HALT no dispara «daemon inactivo» (1.177 falsos en 42 d)
runpy test_kickstart_bootstrap.py   # el autofix levanta un daemon caído del dominio (kickstart→bootstrap)
runpy test_healthcheck_state_aislado.py  # BTP_STATE_DIR aísla de verdad: la batería no escribe en el estado vivo
runpy test_sys_path_limpio.py       # importar una tool no decide de dónde importan las demás
runpy test_centinela_vencidos.py    # nº14  · los plazos vencidos dejan de ser mudos
runpy test_honestidad_lint_repo.py  # nº10  · el barrido cubre algo (barría 0 documentos)
runpy test_evals_honestidad.py      # nº13  · el sello de evidencia ya tiene golden set
runpy test_coste_repo.py            # nº15  · el gasto se mide en TODO el repo, no solo casa base
runpy test_coste_modelos.py         # 13-sep · todo modelo claude-* gastado tiene precio; se ven subagentes y workflows
runpy test_gasto_tarifa.py          # 24-sep · un modelo sin tarifa se DICE; una sola tabla y un solo matcher
runpy test_saldo_prepago.py         # el estimador del prepago deja de afirmar lo que no sabe
runpy test_bench_jev.py           # 21-sep · a Jev solo sale lo que pasa el borde + fechas/@/URLs
runpy test_bench_modelos.py       # 25-sep · P5: partición, umbral en calibración, freno MLX, 0 textos a disco
runpy test_eval_triage_residuo.py # 21-sep · el set dorado no sale con fechas, URLs, @handles ni números largos
runpy test_radar_orden_jev.py     # 21-sep · Jev solo ordena la cola del radar: nunca archiva ni toca lo cruzado
runpy test_radar_encaje_n1.py     # 22-sep · encaje N1: sin trust o con perfil caducado no sale nada; solo ordena
runpy test_radar_gate_multicohorte.py # 21-sep · un «encaja» en un ensayo multicohorte exige citar SU cohorte
runpy test_radar_archivo_cerrados.py # 21-sep · pasar de 200 cierres no borra veredictos ni re-encola leads
runpy test_radar_reintentos.py     # 21-sep · un fallo de red pasajero no deja un tema del radar sin nada
runpy test_session_start_topologia.py  # 24-sep · el HALT del código rojo no hace creer al mini que es el Air

# ⛔ NO añadir aquí (a propósito, no por olvido): test_avisos_origen.py, test_casa_estilo.py,
# test_observatorio.py, test_salida.py, test_tablero.py y test_triage.py importan `salida` SIN
# exportar BTP_TEST_BATTERY, así que meterlos en la batería le mandaría Telegram REAL a {{TITULAR}}.
# test_avisos_origen.py:34 lo dice explícito: «aquí NO ponemos BTP_TEST_BATTERY. Este test
# necesita que salida.send() llegue hasta la boca». Es diseño, no descuido: el 12-jul-2026 una
# pasada de tests le mandó 14 mensajes de verdad. Para engancharlos hace falta antes un modo
# dry/fixture; hasta entonces se corren a mano.

# Meta-check: que este runner no se vuelva a quedar atrás solo.
echo "── meta: tests no invocados ──"
_huerf=""
for _f in "$ROOT"/tests/test_*.py "$ROOT"/tests/test_*.sh; do
  _b=$(basename "$_f")
  case "$_b" in test_avisos_origen.py|test_casa_estilo.py|test_observatorio.py|test_salida.py|test_tablero.py|test_triage.py) continue;; esac
  grep -q "$_b" "$ROOT/tests/test_all.sh" || _huerf="$_huerf $_b"
done
if [ -n "$_huerf" ]; then
  echo "❌ tests escritos que NADIE corre:$_huerf"
  fail=$((fail+1))
else
  echo "✅ ningún test huérfano"
fi

echo
# Un SKIP no es ni verde ni rojo: es «necesita algo que aquí no está» (ver tests/_entorno.py).
# Se dice aparte para que el número de rojos signifique lo que parece.
[ "$skip" -gt 0 ] && echo "⏭️  $skip batería(s) saltada(s): falta el contenido, el estado vivo, los overlays locales o el lazo (HALT activo)"
[ "$fail" -eq 0 ] && echo "✅✅ TODO EN VERDE (muro + lazo P1)" || echo "❌ $fail batería(s) con fallos · logs de ESTA ejecución: $ROJO_DIR"
# Sin rojos, la carpeta propia sobra (nunca /tmp ni una que haya dado el llamador).
[ "$fail" -eq 0 ] && [ -z "$CI" ] && [ -z "$BTP_ROJO_DIR" ] && rmdir "$ROJO_DIR" 2>/dev/null
exit "$fail"
