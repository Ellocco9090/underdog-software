from flask import Flask, render_template, request

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def calcola_pronostico():
    risultato = ""
    validazione_parametri = []
    if request.method == 'POST':
        try:
            vittorie = int(request.form['vittorie'])
            pareggi = int(request.form['pareggi'])
            sconfitte = int(request.form['sconfitte'])
            media_gol_fatti = float(request.form['media_gol_fatti'])
            media_gol_subiti = float(request.form['media_gol_subiti'])
            posizione = int(request.form['posizione'])
            quota_1 = float(request.form['quota_1'])
            quota_x = float(request.form['quota_x'])
            quota_2 = float(request.form['quota_2'])
            gioca_in_casa = request.form['casa_trasferta']

            # Validazione e calcolo motivazioni
            motivazioni = "Media"
            if posizione <= 3 or vittorie >= 2:
                motivazioni = "Alta"
            elif posizione >= 16 and sconfitte > 2:
                motivazioni = "Bassa"

            # Validazione parametri
            validazione_parametri.append("✔️ Vittorie: Almeno 2" if vittorie >= 2 else "❌ Vittorie: Meno di 2")
            validazione_parametri.append("✔️ Media gol fatti: ≥ 1.2" if media_gol_fatti >= 1.2 else "❌ Media gol fatti: < 1.2")
            validazione_parametri.append("✔️ Media gol subiti: ≤ 1.5" if media_gol_subiti <= 1.5 else "❌ Media gol subiti: > 1.5")
            validazione_parametri.append("✔️ Quota sfavorita: ≥ 3.5" if (gioca_in_casa == "Casa" and quota_1 >= 3.5) or (gioca_in_casa == "Trasferta" and quota_2 >= 3.5) else "❌ Quota sfavorita: < 3.5")

            # Calcolo pronostico principale
            pronostico = "Under 2.5"
            if motivazioni == "Alta":
                if gioca_in_casa == "Casa" and quota_1 >= 3.5:
                    pronostico = "1X"
                elif gioca_in_casa == "Trasferta" and quota_2 >= 3.5:
                    pronostico = "X2"

            risultato = f"Motivazione: {motivazioni}\nPronostico: {pronostico}"
        except ValueError:
            risultato = "Errore: inserisci valori validi."

    return render_template('index.html', risultato=risultato, validazione_parametri=validazione_parametri)

if __name__ == '__main__':
    app.run(debug=True)
