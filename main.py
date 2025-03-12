import streamlit as st
import requests
import pandas as pd
import re
from datetime import datetime, timedelta, timezone
from decouple import config
from typing import Tuple
import pytz  # <-- Importamos pytz para manejar zonas horarias

st.set_page_config(layout="wide")  # Modo ancho

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
    """Llamada GET a la API de Canvas con manejo de paginación."""
    full_url = f"{BASE_URL}{endpoint}"
    results = []  # Lista para almacenar todos los resultados

    while full_url:  # Mientras haya una página siguiente
        response = requests.get(full_url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        data = response.json()
        if isinstance(data, list):  # Canvas devuelve listas en respuestas paginadas
            results.extend(data)
        else:
            return data  # Si no es lista, devolver directamente

        # Verificar si hay más páginas en los headers de la respuesta
        full_url = response.links.get("next", {}).get("url")  

    return results  # Devolver la lista completa si había paginación

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

def procesar_curso(course_id: str) -> Tuple[pd.DataFrame, list, dict]:
    """
    Retorna:
      1) DataFrame con 1 columna por tarea (texto con estado/nota).
      2) Lista de tareas procesadas (las que tienen fecha de entrega).
      3) Diccionario con info de Profesor, Tutor, Director.
    
    Lógica en cada celda:
      - "No aplica aun"                -> si now_utc < due_date_utc
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
            estado_info = "NO APLICA AUN"
        else:
            if now_utc > deadline_utc:
                estado_info = "VENCIDO"
            else:
                estado_info = "EN PLAZO"

        # st.info con las fechas locales
        st.info(
            f"**{asg_name}** - **Fecha de entrega:** :green[{fecha_entrega_str}] "
            f"- **Plazo de calificación:** :green[{plazo_calif_str}] - **:red[{estado_info}]**"
        )

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
                text_celda = "No aplica aun"
            elif graded_at:
                # Calificada => nota en verde
                score = submission.get("score") if submission else None
                try:
                    text_celda = str(int(float(score))) if score is not None else "0"
                except:
                    text_celda = "0"
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

    df = pd.DataFrame.from_dict(
        {students[sid]: data for sid, data in results.items()},
        orient="index"
    )

    # Info de enrollments (profesor, tutor, director)
    teacher_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "TeacherEnrollment", "per_page": 100}
    )
    if teacher_data:
        t = teacher_data[0]
        teacher_name = t.get("user", {}).get("name", "no existe")
        teacher_email = t.get("user", {}).get("login_id", "no existe")
    else:
        teacher_name, teacher_email = "no existe", "no existe"

    tutor_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "Tutor social", "per_page": 100}
    )
    if tutor_data:
        tu = tutor_data[0]
        tutor_email = tu.get("user", {}).get("login_id", "No existe")
    else:
        tutor_email = "No existe"

    director_data = fetch_canvas_api(
        f"/courses/{course_id}/enrollments",
        params={"role[]": "Director", "per_page": 100}
    )
    if director_data:
        d = director_data[0]
        director_name = d.get("user", {}).get("name", "No existe")
    else:
        director_name = "No existe"

    info_curso = {
        "Profesor": teacher_name,
        "Correo Profesor": teacher_email,
        "Tutor": tutor_email,
        "Director": director_name
    }

    return df, processed_assignments, info_curso

def style_celda(val: str):
    """Colores según el valor en la celda."""
    v = val.strip().lower()
    if v == "no aplica aun":
        return "background-color: black; color: white"
    if v.isdigit():
        return "background-color: lightgreen; color: black"
    if v in ["entregado y en plazo", "no entregado pero en plazo"]:
        return "background-color: lightblue; color: black"
    if v in ["no calificado en plazo"]:
        return "background-color: yellow; color: black"
    if v in ["no entrego nada"]:
        return "background-color: red; color: white"
    return ""

# ---------------------------------------------------------------------
# Interfaz principal
# ---------------------------------------------------------------------
st.title("VERIFICADOR de calificaciones en Canvas")
st.info("Sirve para ver si los profes han puesto las notas a tiempo o no.")

raw_input = st.text_area(
    "IDs de curso (separados por coma, espacio o salto de línea):",
    placeholder="Ej: 123456, 234567 345678\n456789"
)

if st.button("Revisar calificaciones!"):
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
            sub_account_info = fetch_canvas_api(f"/accounts/{course_info.get('account_id')}")

            st.markdown(
                f"### [{sub_account_info.get('name')} - ({sub_account_info.get('id')})]"
                f"(https://canvas.uautonoma.cl/accounts/{sub_account_info.get('id')})",
                unsafe_allow_html=True
            )
            st.markdown(
                f"##### [{course_info.get('name')} - ({course_info.get('id')}) - "
                f"{course_info.get('course_code')}](https://canvas.uautonoma.cl/courses/{cid}/gradebook)",
                unsafe_allow_html=True
            )
            try:
                df, asg_ok, info_curso = procesar_curso(cid)
                
                # Info del curso
                st.markdown(
                    f"**Profesor:** {info_curso.get('Profesor')} | "
                    f"**Correo:** {info_curso.get('Correo Profesor')}<br>"
                    f"**Tutor:** {info_curso.get('Tutor')} | "
                    f"**Director:** {info_curso.get('Director')}",
                    unsafe_allow_html=True
                )
                if df is not None and not df.empty:
                    styler = df.style.map(style_celda)
                    st.write(styler.to_html(), unsafe_allow_html=True)

                    # 1) Contar cuántos alumnos están fuera de plazo
                    outside_plazo_count = 0
                    for val in df.values.flatten():
                        if val.lower() in ["no calificado en plazo", "no entrego nada"]:
                            outside_plazo_count += 1

                    st.write(f"**Faltan por calificar (fuera de plazo):** {outside_plazo_count}")

                else:
                    st.info("No se procesaron asignaciones con fecha de entrega.")
                    outside_plazo_count = 0

                # Resumen final
                if not asg_ok:
                    estado = "No tiene fechas configuradas"
                    color_estado = "yellow"
                else:
                    all_values = df.values.flatten().tolist()
                    if any(v.lower() in ["no calificado en plazo", "no entrego nada"] for v in all_values):
                        estado = "Hay cosas mal"
                        color_estado = "red"
                    else:
                        estado = "Todo Bien"
                        color_estado = "lightgreen"

                # Agregamos el conteo 'outside_plazo_count' al resumen
                resumen.append({
                    "Curso": cid,
                    "Nombre":course_info.get("name"),
                    "Diplomado":sub_account_info.get("name"),
                    "Profesor": info_curso.get("Profesor"),
                    "Correo Profesor": info_curso.get("Correo Profesor"),
                    "Tutor": info_curso.get("Tutor"),
                    "Director": info_curso.get("Director"),
                    "Faltan fuera de plazo": outside_plazo_count,
                    "Estado": estado,
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
            st.write(styler_resumen.to_html(escape=False), unsafe_allow_html=True)

        st.markdown(f"**Tiempo total del proceso:** {tiempo_total:.2f} segundos")
