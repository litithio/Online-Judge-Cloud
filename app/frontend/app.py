import streamlit as st
import requests
import time
import os

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="Coding Platform", layout="wide")
st.title("💻 Python Code Evaluator")

# Token Handling (Simulierter OAuth Token oder Eingabe für Demo)
if "token" not in st.session_state:
    st.session_state["token"] = ""

with st.sidebar:
    st.header("🔑 Auth")
    st.session_state["token"] = st.text_input("Keycloak Bearer Token", type="password")

if not st.session_state["token"]:
    st.warning("Bitte füge einen gültigen Keycloak JWT Token ein, um fortzufahren.")
    st.stop()

headers = {"Authorization": f"Bearer {st.session_state['token']}"}

# 1. Aufgaben laden
try:
    response = requests.get(f"{BACKEND_URL}/tasks", headers=headers)
    if response.status_code == 401:
        st.error("Token ungültig oder abgelaufen.")
        st.stop()
    tasks = response.json()
except Exception as e:
    st.error(f"Verbindung zum Backend fehlgeschlagen: {e}")
    st.stop()

task_dict = {t["title"]: t["id"] for t in tasks}
selected_title = st.selectbox("Wähle eine Programmieraufgabe:", list(task_dict.keys()))

if selected_title:
    task_id = task_dict[selected_title]
    task_detail = requests.get(f"{BACKEND_URL}/tasks/{task_id}", headers=headers).json()

    st.subheader(task_detail["title"])
    st.write(task_detail["description"])

    # Code Editor
    default_code = "def solution():\n    # Schreibe hier deinen Code\n    pass\n"
    user_code = st.text_area("Dein Python Code:", value=default_code, height=250)

    if st.button("Code Abschicken", type="primary"):
        submit_res = requests.post(
            f"{BACKEND_URL}/submit",
            json={"task_id": task_id, "code": user_code},
            headers=headers
        )
        
        if submit_res.status_code == 200:
            sub_id = submit_res.json()["submission_id"]
            
            # Polling für das Ergebnis
            with st.spinner("Code wird in isolierter Sandbox ausgeführt..."):
                while True:
                    time.sleep(1)
                    status_res = requests.get(f"{BACKEND_URL}/submission/{sub_id}", headers=headers).json()
                    
                    if status_res["status"] != "PENDING":
                        break
            
            # Anzeige des Ergebnisses
            if status_res["status"] == "SUCCESS":
                st.success("🎉 ERFOLG! " + status_res["result"])
            else:
                st.error("❌ FEHLERHAFT: " + status_res["result"])