import streamlit as st
import requests
import pandas as pd
import re
from datetime import datetime, timedelta, timezone
from decouple import config
from typing import Tuple
import pytz  # <-- Importamos pytz para manejar zonas horarias

st.set_page_config(layout="wide", page_title="Supervision de notas al dia 💯", page_icon="💯")  # Modo ancho

# Ajusta a tu zona horaria
tz_local = pytz.timezone("America/Santiago")

# Configuración de Canvas
BASE_URL = "https://canvas.uautonoma.cl/api/v1"
API_TOKEN = config("TOKEN")
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

def fetch_canvas_api(endpoint, params=None):
    full_url = f"{BASE_URL}{endpoint}"
    results = []
    
    response = requests.get(full_url, headers=HEADERS, params=params)
    if response.status_code == 404:
        return None 
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        return data 

    results.extend(data)
    while response.links.get("next"):
        url = response.links["next"]["url"]
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        results.extend(response.json())

    return results

def es_entrega_real(submission: dict) -> bool:
    """
    Retorna True si 'submission' indica que el alumno realmente entregó.
    Se considera 'entregado' si:
      - workflow_state == "submitted", o
      - submitted_at != None
    """
    if not submission:
        return False
    w_state = submission.get("workflow_state", "")
    submitted_at = submission.get("submitted_at")
    return (w_state == "submitted") or (submitted_at is not None)

def obtener_rol_info(course_id: str, role: str) -> Tuple[str, str]:
    data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": role, "per_page": 100}
    )
    if not data:
        return "No existe", "No existe"
    # Extrae nombres y correos
    names  = [e.get("user",{}).get("name",       "No existe") for e in data]
    emails = [e.get("user",{}).get("login_id",   "No existe") for e in data]
    # Une en un solo string
    return ", ".join(names), ", ".join(emails)

def procesar_curso(course_id: str) -> Tuple[pd.DataFrame, list, dict]:
    """
    Retorna:
      1) DataFrame con 1 columna por tarea (texto con estado/nota).
      2) Lista de tareas procesadas (las que tienen fecha de entrega).
      3) Diccionario con info de Profesor, Tutor, Director.
    
    Lógica en cada celda:
      - "No iniciado"                -> si now_utc < due_date_utc
      - Si graded_at existe            -> nota en verde
      - Si no calificado:
         * en plazo (<= due_date_utc+9):
             - "Entregado y en plazo" / "No entregado pero en plazo"
         * fuera de plazo (> due_date_utc+9):
             - "No calificado en plazo" / "No entrego nada"

    Ajuste:
    - Se hace la comparación en UTC (now_utc, due_date_utc, deadline_utc)
    - Para mostrar (fecha_entrega_str, plazo_calif_str), convertimos a la zona horaria local.
    """

    # 1) Alumnos
    enrollments = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"type[]": "StudentEnrollment", "per_page": 100}
    )
    students = {}
    for e in enrollments:
        sid = e.get("user_id")
        uname = e.get("user", {}).get("name", f"User {sid}")
        students[sid] = uname
    if not students:
        st.warning(f"No se encontraron estudiantes para el curso {course_id}.")
        return None, [], {}

    # 2) Tareas
    assignments = fetch_canvas_api(
        f"/courses/{course_id}/assignments",
        params={"per_page": 100}
    )

    processed_assignments = []
    results = {sid: {} for sid in students}

    now_utc = datetime.now(timezone.utc)  # Momento actual en UTC

    asignaciones_info = []
    now_local_date = datetime.now(tz_local).date()
    
    for assignment in assignments:
        asg_id = assignment.get("id")
        asg_name = assignment.get("name")
        due_at = assignment.get("due_at")
        if not due_at:
            st.warning(f"La tarea '{asg_name}' (ID: {asg_id}) no tiene fecha de entrega y se omitirá.")
            continue
        
        # Convertimos la fecha a datetime en UTC
        due_date_utc = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        # Sigues usando +9 días según tu código
        deadline_utc = due_date_utc + timedelta(days=9)

        # Convertimos a hora local para mostrar al usuario:
        due_date_local = due_date_utc.astimezone(tz_local)
        deadline_local = deadline_utc.astimezone(tz_local)

        # Formato que se muestra en la interfaz
        fecha_entrega_str = due_date_local.strftime('%d/%m/%Y')
        plazo_calif_str   = deadline_local.strftime('%d/%m/%Y')

        # Lógica en UTC para decidir el "estado_info"
        if due_date_utc > now_utc:
            estado_info = "No iniciado"
        else:
            if now_utc > deadline_utc:
                estado_info = "Plazo vencido"
            else:
                estado_info = "En plazo"
                
        dias_atraso = (now_local_date - deadline_local.date()).days
        if dias_atraso < 0:
            dias_atraso = 0

        # st.info con las fechas locales
        asignaciones_info.append({
            "Tarea": asg_name,
            "Fecha de entrega": fecha_entrega_str,
            "Plazo de calificación": plazo_calif_str,
            "Días de atraso": dias_atraso if estado_info == "Plazo vencido" else "No aplica",
            "Estado": estado_info
        })

        processed_assignments.append(assignment)

        # Submissions
        submissions = fetch_canvas_api(
            f"/courses/{course_id}/assignments/{asg_id}/submissions",
            params={"per_page": 100}
        )
        subs_map = {s.get("user_id"): s for s in submissions}

        for sid in students:
            submission = subs_map.get(sid)
            delivered = es_entrega_real(submission)
            graded_at = submission.get("graded_at") if submission else None

            # Comparación en UTC
            if now_utc < due_date_utc:
                text_celda = "No iniciado"
            elif graded_at:
                # Si Canvas dice "graded" pero no hay score, mostramos "-"
                score = submission.get("score")
                if score is None:
                    text_celda = "Calificada pero sin nota"
                elif submission.get("grade_matches_current_submission") is False:
                    text_celda = "Nota no coincide"
                else:
                    # Convertimos a entero sólo si score es un número válido
                    text_celda = str(int(float(score)))
            else:
                # No calificado
                if now_utc <= deadline_utc:
                    if delivered:
                        text_celda = "Entregado y en plazo"
                    else:
                        text_celda = "No entregado pero en plazo"
                else:
                    if delivered:
                        text_celda = "No calificado en plazo"
                    else:
                        text_celda = "No entrego nada"

            results[sid][asg_name] = text_celda
            
    if asignaciones_info:
        df_asg = pd.DataFrame(asignaciones_info)
        # Si quieres colorear la columna "Estado":
        def color_estado_asg(v):
            v = v.lower()
            if v == "no iniciado":                return "background-color: black; color: white"
            if v == "en plazo":                   return "background-color: lightgreen"
            if v == "plazo vencido":             return "background-color: lightcoral"
            return ""
        styler = df_asg.style.map(color_estado_asg, subset=["Estado"])
        st.dataframe(styler, use_container_width=False, hide_index=True)
    else:
        st.info("No hay tareas con fecha de entrega.")

    df = pd.DataFrame.from_dict(
        {students[sid]: data for sid, data in results.items()},
        orient="index"
    )

    # Info de enrollments (profesor, tutor, director)
    # Profesores
    teacher_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "TeacherEnrollment", "per_page": 100}
    )
    teacher_names = []
    teacher_emails = []
    if teacher_data:
        for t in teacher_data:
            user = t.get("user", {})
            teacher_names.append(user.get("name", "No existe"))
            teacher_emails.append(user.get("login_id", "No existe"))
    else:
        teacher_names = ["No existe"]
        teacher_emails = ["No existe"]

    # Tutores
    tutor_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "Tutor social", "per_page": 100}
    )
    tutor_emails = []
    if tutor_data:
        for tu in tutor_data:
            user = tu.get("user", {})
            tutor_emails.append(user.get("login_id", "No existe"))
    else:
        tutor_emails = ["No existe"]

    # Directores
    director_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "Director", "per_page": 100}
    )
    director_names = []
    director_emails = []
    if director_data:
        for d in director_data:
            user = d.get("user", {})
            director_names.append(user.get("name", "No existe"))
            director_emails.append(user.get("login_id", "No existe"))
    else:
        director_names = ["No existe"]
        director_emails = ["No existe"]

    # Crear string unificado si quieres mostrarlos como texto
    info_curso = {
        "Profesor": ", ".join(teacher_names),
        "Correo Profesor": ", ".join(teacher_emails),
        "Tutor": ", ".join(tutor_emails),
        "Director": ", ".join(director_names),
        "Correo Director": ", ".join(director_emails)
    }

    return df, processed_assignments, info_curso

def style_celda(val: str):
    """Colores según el valor en la celda."""
    v = val.strip().lower()
    if v == "No iniciado":
        return "background-color: black; color: white"
    if v.isdigit():
        return "background-color: lightgreen; color: black"
    if v in ["entregado y en plazo", "no entregado pero en plazo"]:
        return "background-color: lightblue; color: black"
    if v == "no calificado en plazo":
        return "background-color: yellow; color: black"
    if v == "no entrego nada":
        return "background-color: red; color: white"
    if v == "nota no coincide":
        return "background-color: orange; color: black"
    if v == "calificada pero sin nota":
        return "background-color: orange; color: black"
    return ""

st.title("Supervision de notas al dia 💯")
st.success("Con esta herramientasa puedes revisar el estado de las calificaciones de tus cursos a supervisar en Canvas.")
#st.info("Ultima correcion: Cambiado la forma de buscar al profesor de type[] a role[].")

raw_input = st.text_area(
    "IDs de curso (separados por coma, espacio o salto de línea):",
    placeholder="Ej: 123456, 234567 345678\n456789", height=200
)

if st.button("Revisar!!", use_container_width=True):
    inicio_total = datetime.now()
    course_ids = [c.strip() for c in re.split(r"[\s,]+", raw_input) if c.strip()]
    if not course_ids:
        st.error("Por favor, ingresa al menos un ID de curso.")
    else:
        resumen = []
        for cid in course_ids:
            st.divider()
            # Info extra del curso (opcional)
            course_info = fetch_canvas_api(f"/courses/{cid}")
            if not course_info:
                st.error(f"Curso {cid} no encontrado.")
                resumen.append({
                    "Estado": "Inexistente",
                    "Errores": "No encontrado",
                    "Curso": "No encontrado",
                    "Nombre":"No encontrado",
                    "Diplomado":"No encontrado",
                    "Profesor": "No encontrado",
                    "Email Profesor":"No encontrado",
                    "Director": "No encontrado",
                    "Email Director": "No encontrado",
                    "Tutor": "No encontrado",
                    "Color": "red"
                })
                continue
            sub_account_info = fetch_canvas_api(f"/accounts/{course_info.get('account_id')}")

            st.markdown(
                f"##### [({course_info.get('id')}) {course_info.get('name')} / "
                f"{course_info.get('course_code')}](https://canvas.uautonoma.cl/courses/{cid}/gradebook)",
                unsafe_allow_html=True
            )
            st.markdown(
                f"###### [({sub_account_info.get('id')}) {sub_account_info.get('name')}]"
                f"(https://canvas.uautonoma.cl/accounts/{sub_account_info.get('id')})",
                unsafe_allow_html=True
            )
            try:
                df, asg_ok, info_curso = procesar_curso(cid)
                
                # Info del curso
                # st.markdown(
                #     f"**Profesor:** {info_curso.get('Profesor')} | "
                #     f"**Correo:** {info_curso.get('Correo Profesor')}<br>"
                #     f"**Tutor:** {info_curso.get('Tutor')} | "
                #     f"**Director:** {info_curso.get('Director')}",
                #     unsafe_allow_html=True
                # )
                lista_info = []
                lista_info.append({
                    "Profesor": info_curso["Profesor"],
                    "Email Profesor":   info_curso["Correo Profesor"],
                    "Director": info_curso["Director"],
                    "Email Director": info_curso["Correo Director"],
                    "Email Tutor":    info_curso["Tutor"],
                })
                df_resumen_info = pd.DataFrame(lista_info)
                st.dataframe(df_resumen_info, use_container_width=True, hide_index=True)
                
                if df is not None and not df.empty:
                    styler = df.style.map(style_celda)
                    #st.write(styler.to_html(), unsafe_allow_html=True)
                    st.dataframe(styler, use_container_width=True)

                    # Contar cuántos alumnos están fuera de plazo
                    outside_plazo_count = 0
                    for val in df.values.flatten():
                        if val.lower() in ["no calificado en plazo", "no entrego nada", "nota no coincide", "calificada pero sin nota"]:
                            outside_plazo_count += 1

                    st.write(f"**Notas fuera de plazo:** {outside_plazo_count}")

                else:
                    st.info("No se procesaron asignaciones con fecha de entrega.")
                    outside_plazo_count = 0

                # Resumen final
                if not asg_ok:
                    estado = "No configurado"
                    color_estado = "orange"
                else:
                    all_values = df.values.flatten().tolist()
                    #print(all_values)
                    if any(str(v).strip().lower() in ["no calificado en plazo", "no entrego nada", "nota no coincide", "calificada pero sin nota"] for v in all_values):
                        estado = "Hay cosas mal"
                        color_estado = "red"
                    else:
                        estado = "Todo Bien"
                        color_estado = "lightgreen"

                # Agregamos el conteo 'outside_plazo_count' al resumen
                resumen.append({
                    "Estado": estado,
                    "Errores": outside_plazo_count,
                    "Curso": cid,
                    "Nombre":course_info.get("name"),
                    "Diplomado":sub_account_info.get("name"),
                    "Profesor": info_curso.get("Profesor"),
                    "Email Profesor": info_curso.get("Correo Profesor"),
                    "Director": info_curso.get("Director"),
                    "Email Director": info_curso.get("Correo Director"),
                    "Tutor": info_curso.get("Tutor"),
                    "Color": color_estado
                })

            except Exception as e:
                st.error(f"Error procesando curso {cid}: {e}")

        fin_total = datetime.now()
        tiempo_total = (fin_total - inicio_total).total_seconds()

        if resumen:
            st.markdown("## Resumen de cursos")
            df_resumen = pd.DataFrame(resumen).drop(columns=["Color"], errors="ignore")

            def style_resumen_cell(val):
                row = next((r for r in resumen if r["Estado"] == val), None)
                if row:
                    return f"background-color: {row['Color']};"
                return ""

            styler_resumen = df_resumen.style.map(style_resumen_cell, subset=["Estado"])
            #st.write(styler_resumen.to_html(escape=False), unsafe_allow_html=True)
            st.dataframe(styler_resumen, use_container_width=True, hide_index=False)

        st.markdown(f"**Tiempo total del proceso:** {tiempo_total:.2f} segundos")
