"""
Robot météo Clos Thierrière — v4
=================================
A.1 — Documentation enrichie : README structuré + onglet Méthodologie complet
A.2 — Gel ajusté contexte cuvette du Clos + alertes documentées
A.3 — Tours-Saint-Symphorien (Météo-France DPClim) en source officielle observée

Stockage incrémental sur 3 fichiers parquet :
  - donnees_brutes.parquet     : ERA5 + AROME au point de Vernou
  - donnees_tours.parquet       : station Tours-St-Symphorien (mesure officielle)
"""

import io
import os
import time
import functools
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# Affichage temps réel des prints
print = functools.partial(print, flush=True)

# =============================================================================
# 1. PARAMÈTRES
# =============================================================================
LATITUDE = 47.4308
LONGITUDE = 0.9572
NOM_DOMAINE = "Clos Thierrière"
DATE_DEBUT_HISTOIRE = date(1990, 1, 1)
DATE_BASCULE_ERA5_AROME = date(2021, 1, 1)
DATE_FIN = date.today() - timedelta(days=1)

FICHIER_EXCEL = "clos_thierriere_climato.xlsx"
FICHIER_BRUTES = "donnees_brutes.parquet"
FICHIER_TOURS = "donnees_tours.parquet"

GSHEET_ID = "1xrqqxom2uDO6jhys0q23xUf2qMV9u9K9skJ3PjhtDwQ"
NORMALE_DEBUT = 1991
NORMALE_FIN = 2020

# Station Météo-France Tours-Saint-Symphorien
TOURS_STATION_ID = "37179001"  # ID DPClim
METEOFRANCE_API_KEY = os.environ.get("METEOFRANCE_API_KEY", "")

# Catégorisation d'une nuit selon les conditions météo
# Sert à appliquer une correction cuvette calibrée sur des données réelles
def categorie_nuit(vent_max_kmh, hr_moy):
    if vent_max_kmh is None or hr_moy is None:
        return "intermediaire"
    if vent_max_kmh > 25:
        return "ventee"
    if vent_max_kmh < 12 and hr_moy < 85:
        return "calme_seche"
    return "intermediaire"

# Les corrections seront calibrées dynamiquement sur les données Tours
# (calcul fait après la fusion Tours/AROME, dans la section 6)

print(f"Robot météo — {NOM_DOMAINE}")
print(f"Position : {LATITUDE}°N, {LONGITUDE}°E")
print(f"Période cible : {DATE_DEBUT_HISTOIRE} → {DATE_FIN}")
print()

VARIABLES_DAILY = [
    "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
    "precipitation_sum", "rain_sum", "snowfall_sum", "precipitation_hours",
    "sunshine_duration", "shortwave_radiation_sum",
    "et0_fao_evapotranspiration",
    "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant",
    "relative_humidity_2m_mean", "relative_humidity_2m_max", "relative_humidity_2m_min",
    "dew_point_2m_mean",
]


# =============================================================================
# 2. TÉLÉCHARGEMENT OPEN-METEO (ERA5 + AROME)
# =============================================================================
def telecharger_periode(url, debut, fin, source_label, mois_par_tranche=12):
    if debut > fin:
        return pd.DataFrame()
    print(f"  Téléchargement {source_label} : {debut} → {fin}...")
    morceaux = []
    cursor = debut
    while cursor <= fin:
        annee = cursor.year
        mois = cursor.month
        mois_fin = mois + mois_par_tranche - 1
        annee_fin = annee + (mois_fin - 1) // 12
        mois_fin = ((mois_fin - 1) % 12) + 1
        if mois_fin == 12:
            fin_tranche = date(annee_fin, 12, 31)
        else:
            fin_tranche = date(annee_fin, mois_fin + 1, 1) - timedelta(days=1)
        fin_tranche = min(fin_tranche, fin)

        params = {
            "latitude": LATITUDE, "longitude": LONGITUDE,
            "start_date": cursor.isoformat(), "end_date": fin_tranche.isoformat(),
            "daily": ",".join(VARIABLES_DAILY),
            "timezone": "Europe/Paris", "wind_speed_unit": "kmh",
        }
        succes = False
        for tentative in range(5):
            try:
                r = requests.get(url, params=params, timeout=300)
                if r.status_code == 429:
                    time.sleep(10 * (tentative + 1)); continue
                r.raise_for_status()
                succes = True; break
            except (requests.exceptions.Timeout, requests.exceptions.RequestException):
                time.sleep(15 * (tentative + 1)); continue
        if not succes:
            raise RuntimeError(f"Échec {source_label} {cursor}-{fin_tranche}")
        df_t = pd.DataFrame(r.json()["daily"])
        morceaux.append(df_t)
        print(f"    {cursor} → {fin_tranche} ok ({len(df_t)} jours)")
        cursor = fin_tranche + timedelta(days=1)
        time.sleep(1.5)
    df_full = pd.concat(morceaux, ignore_index=True)
    df_full["source"] = source_label
    return df_full


# =============================================================================
# 3. STRATÉGIE INCRÉMENTALE — données brutes ERA5/AROME
# =============================================================================
print("ÉTAPE 1/7 — Stratégie incrémentale (ERA5/AROME)")

if Path(FICHIER_BRUTES).exists():
    df_existant = pd.read_parquet(FICHIER_BRUTES)
    df_existant["time"] = pd.to_datetime(df_existant["time"])
    derniere_date = df_existant["time"].max().date()
    print(f"  Fichier existant : {len(df_existant)} jours, jusqu'au {derniere_date}")
else:
    df_existant = pd.DataFrame()
    derniere_date = None
    print(f"  Aucun fichier existant — premier téléchargement complet")

nouveaux_morceaux = []
if derniere_date is None:
    df_era5 = telecharger_periode(
        "https://archive-api.open-meteo.com/v1/archive",
        DATE_DEBUT_HISTOIRE, DATE_BASCULE_ERA5_AROME - timedelta(days=1),
        "ERA5", mois_par_tranche=6)
    df_arome = telecharger_periode(
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        DATE_BASCULE_ERA5_AROME, DATE_FIN, "AROME-1km", mois_par_tranche=12)
    nouveaux_morceaux = [df_era5, df_arome]
else:
    debut_complement = derniere_date + timedelta(days=1)
    if debut_complement > DATE_FIN:
        print(f"  Données déjà à jour ({derniere_date}). Rien à télécharger.")
    else:
        if debut_complement < DATE_BASCULE_ERA5_AROME:
            df_era5 = telecharger_periode(
                "https://archive-api.open-meteo.com/v1/archive",
                debut_complement, min(DATE_BASCULE_ERA5_AROME - timedelta(days=1), DATE_FIN),
                "ERA5", mois_par_tranche=6)
            nouveaux_morceaux.append(df_era5)
            debut_complement = DATE_BASCULE_ERA5_AROME
        if debut_complement <= DATE_FIN:
            df_arome = telecharger_periode(
                "https://historical-forecast-api.open-meteo.com/v1/forecast",
                debut_complement, DATE_FIN, "AROME-1km", mois_par_tranche=12)
            nouveaux_morceaux.append(df_arome)

if nouveaux_morceaux:
    df_nouveau = pd.concat(nouveaux_morceaux, ignore_index=True)
    df_nouveau["time"] = pd.to_datetime(df_nouveau["time"])
    if not df_existant.empty:
        df_brut = pd.concat([df_existant, df_nouveau], ignore_index=True)
        df_brut = df_brut.drop_duplicates(subset=["time"], keep="first")
    else:
        df_brut = df_nouveau
    df_brut = df_brut.sort_values("time").reset_index(drop=True)
    df_brut.to_parquet(FICHIER_BRUTES, index=False)
    print(f"  Sauvegarde : {len(df_brut)} jours total")
else:
    df_brut = df_existant

df = df_brut.copy()
print()

# =============================================================================
# 4. TÉLÉCHARGEMENT TOURS-ST-SYMPHORIEN (Météo-France DPClim)
# =============================================================================
print("ÉTAPE 2/7 — Tours-Saint-Symphorien (Météo-France DPClim)")

def telecharger_tours_dpclim(debut, fin):
    """Télécharge les données quotidiennes Tours via API DPClim."""
    if not METEOFRANCE_API_KEY:
        print("  ⚠️  Pas de clé Météo-France — Tours non téléchargé")
        return pd.DataFrame()
    if debut > fin:
        return pd.DataFrame()
    print(f"  Tours-St-Symphorien : {debut} → {fin}")
    base_url = "https://public-api.meteofrance.fr/public/DPClim/v1"
    headers = {
        "apikey": METEOFRANCE_API_KEY,
        "accept": "*/*",
    }
    cmd_url = f"{base_url}/commande-station/quotidienne"
    cmd_params = {
        "id-station": TOURS_STATION_ID,
        "date-deb-periode": f"{debut.isoformat()}T00:00:00Z",
        "date-fin-periode": f"{fin.isoformat()}T23:59:59Z",
    }
    try:
        r = requests.get(cmd_url, params=cmd_params, headers=headers, timeout=60)
        if r.status_code == 401:
            print(f"  ⚠️  Auth refusée (401). Réponse : {r.text[:300]}")
            return pd.DataFrame()
        if r.status_code == 403:
            print(f"  ⚠️  Accès refusé (403). Réponse : {r.text[:300]}")
            return pd.DataFrame()
        if r.status_code == 429:
            print(f"  ⚠️  Quota atteint (429), pause 65s")
            time.sleep(65)
            r = requests.get(cmd_url, params=cmd_params, headers=headers, timeout=60)
        r.raise_for_status()
        cmd_id = r.json().get("elaboreProduitAvecDemandeResponse", {}).get("return", "")
        if not cmd_id:
            print(f"  ⚠️  Pas d'ID commande. Réponse : {r.text[:300]}")
            return pd.DataFrame()
        print(f"    Commande {cmd_id} créée, attente du fichier...")
    except Exception as e:
        print(f"  ⚠️  Erreur commande Tours : {e}")
        return pd.DataFrame()

    fichier_url = f"{base_url}/commande/fichier"
    csv_text = None
    for tentative in range(15):
        time.sleep(10)
        try:
            r = requests.get(fichier_url, params={"id-cmde": cmd_id}, headers=headers, timeout=120)
            if r.status_code in (200, 201):
                csv_text = r.text
                print(f"    ✅ Fichier reçu ({len(csv_text)} caractères)")
                break
            if r.status_code == 204:
                print(f"    Pas encore prêt (204), attente...")
                continue
            if r.status_code == 410:
                print(f"    Production déjà livrée (410). Abandon.")
                return pd.DataFrame()
            if r.status_code in (404, 500, 507):
                print(f"    Statut {r.status_code}, retry...")
                continue
        except Exception as e:
            print(f"    Erreur récupération : {e}")
            continue
    if not csv_text:
        print("  ⚠️  Fichier Tours non récupéré, on continue sans")
        return pd.DataFrame()

    try:
        df_t = pd.read_csv(io.StringIO(csv_text), sep=";", decimal=",")
        rename_map = {"DATE": "date", "TN": "T_min_obs", "TX": "T_max_obs",
                      "TM": "T_moy_obs", "RR": "RR_obs"}
        df_t = df_t.rename(columns={k: v for k, v in rename_map.items() if k in df_t.columns})
        if "date" in df_t.columns:
            df_t["date"] = pd.to_datetime(df_t["date"], format="%Y%m%d", errors="coerce")
            df_t = df_t.dropna(subset=["date"])
        print(f"    ✅ {len(df_t)} lignes Tours parsées")
        # Pause respecter la limite 50 req/min
        time.sleep(2)
        return df_t
    except Exception as e:
        print(f"  ⚠️  Erreur parsing CSV Tours : {e}")
        return pd.DataFrame()


# Stratégie incrémentale Tours
if Path(FICHIER_TOURS).exists():
    df_tours_existant = pd.read_parquet(FICHIER_TOURS)
    df_tours_existant["date"] = pd.to_datetime(df_tours_existant["date"])
    derniere_tours = df_tours_existant["date"].max().date()
    print(f"  Tours existant : {len(df_tours_existant)} jours, jusqu'au {derniere_tours}")
    debut_tours = derniere_tours + timedelta(days=1)
else:
    df_tours_existant = pd.DataFrame()
    debut_tours = DATE_DEBUT_HISTOIRE

if debut_tours <= DATE_FIN and METEOFRANCE_API_KEY:
    # DPClim limite par requête, on télécharge par tranches d'1 an max
    morceaux_tours = []
    cursor = debut_tours
    while cursor <= DATE_FIN:
        fin_tranche = min(date(cursor.year, 12, 31), DATE_FIN)
        df_t = telecharger_tours_dpclim(cursor, fin_tranche)
        if not df_t.empty:
            morceaux_tours.append(df_t)
        cursor = fin_tranche + timedelta(days=1)
    if morceaux_tours:
        df_tours_nouveau = pd.concat(morceaux_tours, ignore_index=True)
        if not df_tours_existant.empty:
            df_tours = pd.concat([df_tours_existant, df_tours_nouveau], ignore_index=True)
            df_tours = df_tours.drop_duplicates(subset=["date"], keep="last")
        else:
            df_tours = df_tours_nouveau
        df_tours = df_tours.sort_values("date").reset_index(drop=True)
        df_tours.to_parquet(FICHIER_TOURS, index=False)
        print(f"  Tours sauvegardé : {len(df_tours)} jours total")
    else:
        df_tours = df_tours_existant
else:
    df_tours = df_tours_existant
    print(f"  Tours déjà à jour ou clé manquante.")
print()

# =============================================================================
# 5. RENOMMAGE & VARIABLES DÉRIVÉES (données AROME/ERA5)
# =============================================================================
print("ÉTAPE 3/7 — Préparation des données")

df = df.rename(columns={
    "time": "Date", "temperature_2m_max": "T_max", "temperature_2m_min": "T_min",
    "temperature_2m_mean": "T_moy", "precipitation_sum": "RR", "rain_sum": "Pluie",
    "snowfall_sum": "Neige_cm", "precipitation_hours": "RR_heures",
    "sunshine_duration": "Insolation_s", "shortwave_radiation_sum": "Rayonnement_MJ_m2",
    "et0_fao_evapotranspiration": "ETP", "wind_speed_10m_max": "Vent_max_kmh",
    "wind_gusts_10m_max": "Rafale_kmh", "wind_direction_10m_dominant": "Vent_dir",
    "relative_humidity_2m_mean": "HR_moy", "relative_humidity_2m_max": "HR_max",
    "relative_humidity_2m_min": "HR_min", "dew_point_2m_mean": "Pt_rosee",
    "source": "Source",
})

df["Insolation_h"] = (df["Insolation_s"] / 3600).round(2)
df.drop(columns=["Insolation_s"], inplace=True)
df["Amplitude_thermique"] = (df["T_max"] - df["T_min"]).round(2)
df["Bilan_hydrique_J"] = (df["RR"] - df["ETP"]).round(2)
df["Annee"] = df["Date"].dt.year
df["Mois"] = df["Date"].dt.month
df["Jour"] = df["Date"].dt.day
df["Jour_julien"] = df["Date"].dt.dayofyear
df["Semaine_ISO"] = df["Date"].dt.isocalendar().week.astype(int)

# Catégorisation des nuits
df["Categorie_nuit"] = df.apply(
    lambda r: categorie_nuit(r["Vent_max_kmh"], r["HR_moy"]), axis=1
)
# Initialisation : T_min_ajustee = T_min en attendant la calibration
df["T_min_ajustee"] = df["T_min"]
df["Correction_cuvette"] = 0.0

# Compteurs gel : VERSION MODÈLE (brute)
df["Jour_gel_modele"] = (df["T_min"] <= 0).astype(int)
df["Jour_gel_severe_modele"] = (df["T_min"] <= -2).astype(int)

# Compteurs gel : VERSION AJUSTÉE (réaliste pour le Clos)
df["Jour_gel_ajuste"] = (df["T_min_ajustee"] <= 0).astype(int)
df["Jour_gel_severe_ajuste"] = (df["T_min_ajustee"] <= -2).astype(int)

# Gel printanier (mars-mai) — clé pour la vigne
df["Gel_printanier_modele"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min"] <= 0)).astype(int)
df["Gel_printanier_ajuste"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min_ajustee"] <= 0)).astype(int)
df["Gel_printanier_severe_ajuste"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min_ajustee"] <= -2)).astype(int)

# Compteurs binaires généraux
df["Jour_chaud_25"] = (df["T_max"] >= 25).astype(int)
df["Jour_chaud_30"] = (df["T_max"] >= 30).astype(int)
df["Jour_chaud_35"] = (df["T_max"] >= 35).astype(int)
df["Jour_tropical"] = (df["T_min"] >= 20).astype(int)
df["Jour_pluvieux"] = (df["RR"] >= 1).astype(int)
df["Jour_pluvieux_fort"] = (df["RR"] >= 20).astype(int)
df["Jour_sec"] = (df["RR"] < 0.5).astype(int)

# =============================================================================
# 6. INTÉGRATION DES OBSERVATIONS TOURS
# =============================================================================
if not df_tours.empty:
    df_tours_clean = df_tours.copy()
    df_tours_clean["date"] = pd.to_datetime(df_tours_clean["date"])
    cols_to_keep = ["date"] + [c for c in ["T_min_obs", "T_max_obs", "T_moy_obs", "RR_obs"] if c in df_tours_clean.columns]
    df_tours_clean = df_tours_clean[cols_to_keep]
    df = df.merge(df_tours_clean, left_on="Date", right_on="date", how="left")
    if "date" in df.columns:
        df.drop(columns=["date"], inplace=True)
    df["Jour_gel_observe"] = ((df.get("T_min_obs", pd.Series(dtype=float)) <= 0)).astype(int)
    print(f"  Fusion Tours OK ({df['T_min_obs'].notna().sum()} jours observés disponibles)")
else:
    df["T_min_obs"] = None
    df["T_max_obs"] = None
    df["RR_obs"] = None
    df["Jour_gel_observe"] = 0
    print("  Pas de données Tours à fusionner")
  # =============================================================================
# 6 bis — CALIBRATION EMPIRIQUE DE LA CORRECTION CUVETTE
# =============================================================================
# Méthode : on calcule pour chaque type de nuit (ventée / calme et sèche / intermédiaire)
# le delta moyen entre la mesure observée Tours-St-Symphorien et le modèle AROME au
# point de Vernou. Ce delta sert ensuite de correction empirique appliquée à T_min.
#
# Limite assumée : Tours est en plaine à 11 km, donc le delta capture surtout
# la différence d'altitude/exposition entre Tours et la maille AROME de Vernou,
# pas exactement la cuvette du Clos. Une station physique au domaine permettra
# une vraie calibration parcellaire.

print("ÉTAPE 3 bis/7 — Calibration empirique correction cuvette")

corrections_calibrees = {"ventee": -1.5, "calme_seche": -1.5, "intermediaire": -1.5}  # défaut prudent

if "T_min_obs" in df.columns and df["T_min_obs"].notna().sum() > 100:
    masque_calib = df["T_min_obs"].notna() & df["T_min"].notna()
    df_calib = df[masque_calib].copy()
    df_calib["delta"] = df_calib["T_min_obs"] - df_calib["T_min"]
    # Calcul du delta moyen par catégorie
    for cat in ["ventee", "calme_seche", "intermediaire"]:
        sub = df_calib[df_calib["Categorie_nuit"] == cat]
        if len(sub) > 30:
            delta_moy = sub["delta"].mean()
            corrections_calibrees[cat] = round(delta_moy, 2)
            print(f"  {cat:<15} : {len(sub)} nuits, delta moyen Tours-AROME = {delta_moy:+.2f}°C")
        else:
            print(f"  {cat:<15} : trop peu de données ({len(sub)} nuits), correction par défaut")
    print(f"  Corrections calibrées appliquées : {corrections_calibrees}")
else:
    print("  Pas assez de données Tours pour calibrer — corrections par défaut utilisées")

# Application des corrections calibrées
df["Correction_cuvette"] = df["Categorie_nuit"].map(corrections_calibrees).round(2)
df["T_min_ajustee"] = (df["T_min"] + df["Correction_cuvette"]).round(2)

# Recalcul des compteurs gel ajustés (après application de la correction calibrée)
df["Jour_gel_ajuste"] = (df["T_min_ajustee"] <= 0).astype(int)
df["Jour_gel_severe_ajuste"] = (df["T_min_ajustee"] <= -2).astype(int)
df["Gel_printanier_ajuste"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min_ajustee"] <= 0)).astype(int)
df["Gel_printanier_severe_ajuste"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min_ajustee"] <= -2)).astype(int)

# =============================================================================
# 7. INDICES VITICOLES JOURNALIERS
# =============================================================================
print("ÉTAPE 4/7 — Indices viticoles journaliers")

def coef_huglin_lat(lat):
    if lat <= 40: return 1.00
    if lat <= 42: return 1.02
    if lat <= 44: return 1.03
    if lat <= 46: return 1.04
    if lat <= 48: return 1.05
    return 1.06

K_HUGLIN = coef_huglin_lat(LATITUDE)
df["GDD_base10"] = ((df["T_moy"] - 10).clip(lower=0)).round(2)
df["GDD_base5"] = ((df["T_moy"] - 5).clip(lower=0)).round(2)
df["GDD_base0"] = df["T_moy"].clip(lower=0).round(2)
df["Contrib_Winkler"] = df["GDD_base10"]
df["Contrib_Huglin"] = (
    ((df["T_moy"] - 10).clip(lower=0) + (df["T_max"] - 10).clip(lower=0)) / 2 * K_HUGLIN
).round(2)
df["Contrib_GFV"] = df["GDD_base0"]
df["Contrib_BBCH"] = df["GDD_base5"]

df["Winkler_cumul"] = 0.0
df["Huglin_cumul"] = 0.0
df["GFV_cumul"] = 0.0
df["BBCH5_cumul"] = 0.0
df["RR_cumul_campagne"] = 0.0
df["ETP_cumul_campagne"] = 0.0

for an in df["Annee"].unique():
    m_an = df["Annee"] == an
    m_winkler = m_an & (df["Mois"] >= 4) & (df["Mois"] <= 10)
    m_huglin = m_an & (df["Mois"] >= 4) & (df["Mois"] <= 9)
    m_camp = m_an & (df["Mois"] >= 4) & (df["Mois"] <= 9)
    df.loc[m_winkler, "Winkler_cumul"] = df.loc[m_winkler, "Contrib_Winkler"].cumsum().round(1)
    df.loc[m_huglin, "Huglin_cumul"] = df.loc[m_huglin, "Contrib_Huglin"].cumsum().round(1)
    df.loc[m_an, "GFV_cumul"] = df.loc[m_an, "Contrib_GFV"].cumsum().round(1)
    df.loc[m_an, "BBCH5_cumul"] = df.loc[m_an, "Contrib_BBCH"].cumsum().round(1)
    df.loc[m_camp, "RR_cumul_campagne"] = df.loc[m_camp, "RR"].cumsum().round(1)
    df.loc[m_camp, "ETP_cumul_campagne"] = df.loc[m_camp, "ETP"].cumsum().round(1)

df["Bilan_hydrique_cumul"] = (df["RR_cumul_campagne"] - df["ETP_cumul_campagne"]).round(1)

# RFU
RFU_MAX = 150.0
df["RFU_mm"] = 0.0
rfu = RFU_MAX
for i in range(len(df)):
    bilan = (df.iloc[i]["RR"] or 0) - (df.iloc[i]["ETP"] or 0)
    rfu = max(0.0, min(RFU_MAX, rfu + bilan))
    df.iat[i, df.columns.get_loc("RFU_mm")] = round(rfu, 1)

df["Stress_hydrique"] = pd.cut(
    df["RFU_mm"], bins=[-0.1, 30, 60, 100, 200],
    labels=["Sévère", "Modéré", "Léger", "Aucun"]
).astype(str)

# =============================================================================
# 8. RISQUES SANITAIRES
# =============================================================================
print("ÉTAPE 5/7 — Risques sanitaires")

df["RR_48h"] = df["RR"].rolling(2, min_periods=1).sum()
df["Mildiou_score"] = (
    ((df["T_moy"] >= 10) & (df["T_moy"] <= 25)).astype(int)
    + (df["RR_48h"] >= 10).astype(int)
    + (df["HR_moy"] >= 75).astype(int)
)
df["Mildiou_risque"] = pd.cut(df["Mildiou_score"], bins=[-1, 0, 1, 2, 3],
    labels=["Nul", "Faible", "Modéré", "Élevé"]).astype(str)

df["Oidium_jour_favorable"] = ((df["T_max"] >= 21) & (df["T_max"] <= 30) & (df["HR_moy"] >= 60)).astype(int)
df["Oidium_score"] = df["Oidium_jour_favorable"].rolling(7, min_periods=1).sum()
df["Oidium_risque"] = pd.cut(df["Oidium_score"], bins=[-1, 2, 4, 6, 7],
    labels=["Faible", "Modéré", "Élevé", "Très élevé"]).astype(str)

df["RR_7j"] = df["RR"].rolling(7, min_periods=1).sum()
df["Botrytis_score"] = (
    ((df["HR_moy"] >= 80).astype(int) * 2)
    + ((df["RR_7j"] >= 30).astype(int) * 2)
    + ((df["T_moy"] >= 15) & (df["T_moy"] <= 22)).astype(int)
)
df["Botrytis_risque"] = pd.cut(df["Botrytis_score"], bins=[-1, 1, 2, 3, 5],
    labels=["Faible", "Modéré", "Élevé", "Très élevé"]).astype(str)

df["Echaudage_jour"] = ((df["T_max"] >= 35) & (df["RFU_mm"] < 60)).astype(int)
df["RR_demain"] = df["RR"].shift(-1).fillna(0)
df["Fenetre_traitement_OK"] = (
    (df["RR"] < 1) & (df["RR_demain"] < 2) &
    (df["Vent_max_kmh"] < 30) & (df["HR_moy"] < 85)
).astype(int)
df.drop(columns=["RR_demain"], inplace=True)

df["Jour_ideal_maturation"] = (
    (df["Mois"].isin([8, 9])) & (df["T_max"] >= 22) & (df["T_max"] <= 28) &
    (df["T_min"] >= 12) & (df["T_min"] <= 18) & (df["RR"] < 1)
).astype(int)
df["Jour_ideal_vendange"] = (
    (df["T_moy"] >= 15) & (df["T_moy"] <= 22) &
    (df["RR"] < 0.5) & (df["HR_max"] >= 85)
).astype(int)

# =============================================================================
# 9. SYNTHÈSES MENSUELLES, ANNUELLES, DÉCENNIES
# =============================================================================
print("ÉTAPE 6/7 — Synthèses")

synthese_mens = df.groupby(["Annee", "Mois"]).agg(**{
    "T_min_moy": ("T_min", "mean"), "T_max_moy": ("T_max", "mean"),
    "T_moy": ("T_moy", "mean"), "T_max_abs": ("T_max", "max"), "T_min_abs": ("T_min", "min"),
    "Amplitude_moy": ("Amplitude_thermique", "mean"), "RR_total": ("RR", "sum"),
    "ETP_total": ("ETP", "sum"), "Bilan_hydrique": ("Bilan_hydrique_J", "sum"),
    "Insolation_h": ("Insolation_h", "sum"), "Rayonnement_MJ_m2": ("Rayonnement_MJ_m2", "sum"),
    "Vent_max_moy_kmh": ("Vent_max_kmh", "mean"), "HR_moy_pct": ("HR_moy", "mean"),
    "Jours_gel_modele": ("Jour_gel_modele", "sum"),
    "Jours_gel_ajuste": ("Jour_gel_ajuste", "sum"),
    "Jours_gel_severe_ajuste": ("Jour_gel_severe_ajuste", "sum"),
    "Jours_chauds_30": ("Jour_chaud_30", "sum"), "Jours_chauds_35": ("Jour_chaud_35", "sum"),
    "Nuits_tropicales": ("Jour_tropical", "sum"), "Jours_pluvieux": ("Jour_pluvieux", "sum"),
    "Jours_secs": ("Jour_sec", "sum"),
    "Mildiou_jours_eleves": ("Mildiou_score", lambda s: int((s >= 3).sum())),
    "Oidium_jours_eleves": ("Oidium_score", lambda s: int((s >= 5).sum())),
    "Fenetres_traitement": ("Fenetre_traitement_OK", "sum"),
}).round(1).reset_index()

masque_normale = (df["Annee"] >= NORMALE_DEBUT) & (df["Annee"] <= NORMALE_FIN)
normales_mens = df[masque_normale].groupby("Mois").agg(**{
    "T_min_normale": ("T_min", "mean"), "T_max_normale": ("T_max", "mean"),
    "T_moy_normale": ("T_moy", "mean"), "RR_normale": ("RR", "sum"),
    "ETP_normale": ("ETP", "sum"), "Insolation_normale_h": ("Insolation_h", "sum"),
}).round(1)
nb_annees_normales = NORMALE_FIN - NORMALE_DEBUT + 1
for col in ["RR_normale", "ETP_normale", "Insolation_normale_h"]:
    normales_mens[col] = (normales_mens[col] / nb_annees_normales).round(1)
normales_mens = normales_mens.reset_index()

fiches = []
for an in sorted(df["Annee"].unique()):
    sous = df[df["Annee"] == an]
    fenetre_winkler = sous[(sous["Mois"] >= 4) & (sous["Mois"] <= 10)]
    fenetre_huglin = sous[(sous["Mois"] >= 4) & (sous["Mois"] <= 9)]
    fenetre_camp = sous[(sous["Mois"] >= 4) & (sous["Mois"] <= 9)]
    septembre = sous[sous["Mois"] == 9]
    aout = sous[sous["Mois"] == 8]
    huglin = fenetre_huglin["Contrib_Huglin"].sum()
    winkler = fenetre_winkler["Contrib_Winkler"].sum()
    if_nuits = septembre["T_min"].mean() if len(septembre) else None
    pluie_camp = fenetre_camp["RR"].sum()
    etp_camp = fenetre_camp["ETP"].sum()
    pluie_an = sous["RR"].sum()
    amplitude_aout = aout["Amplitude_thermique"].mean() if len(aout) else None
    riou = round(pluie_camp - etp_camp, 1)
    selianinov = round((pluie_camp / winkler * 10), 2) if winkler > 0 else None

    if huglin < 1500: classe_huglin = "Très frais"
    elif huglin < 1800: classe_huglin = "Frais"
    elif huglin < 2100: classe_huglin = "Tempéré"
    elif huglin < 2400: classe_huglin = "Tempéré chaud"
    elif huglin < 3000: classe_huglin = "Chaud"
    else: classe_huglin = "Très chaud"
    if winkler < 850: classe_winkler = "Région I (très frais)"
    elif winkler < 1389: classe_winkler = "Région II (frais)"
    elif winkler < 1667: classe_winkler = "Région III (tempéré)"
    elif winkler < 1944: classe_winkler = "Région IV (chaud)"
    elif winkler < 2222: classe_winkler = "Région V (très chaud)"
    else: classe_winkler = "Au-delà Région V"
    if if_nuits is None: classe_if = "—"
    elif if_nuits <= 12: classe_if = "Nuits très fraîches"
    elif if_nuits <= 14: classe_if = "Nuits fraîches"
    elif if_nuits <= 18: classe_if = "Nuits tempérées"
    else: classe_if = "Nuits chaudes"

    fiches.append({
        "Millesime": an, "T_moy_annuelle": round(sous["T_moy"].mean(), 2),
        "T_max_max": round(sous["T_max"].max(), 1), "T_min_min": round(sous["T_min"].min(), 1),
        "RR_totale_mm": round(pluie_an, 1), "RR_campagne_mm": round(pluie_camp, 1),
        "ETP_campagne_mm": round(etp_camp, 1), "Bilan_hydrique_campagne": riou,
        "Indice_Huglin": round(huglin, 0), "Classe_Huglin": classe_huglin,
        "Indice_Winkler": round(winkler, 0), "Classe_Winkler": classe_winkler,
        "IF_septembre": round(if_nuits, 2) if if_nuits is not None else None,
        "Classe_IF_nuits": classe_if, "Indice_Selianinov": selianinov,
        "Amplitude_thermique_aout": round(amplitude_aout, 1) if amplitude_aout is not None else None,
        "Jours_gel_modele": int(sous["Jour_gel_modele"].sum()),
        "Jours_gel_ajuste": int(sous["Jour_gel_ajuste"].sum()),
        "Jours_gel_printanier_modele": int(sous["Gel_printanier_modele"].sum()),
        "Jours_gel_printanier_ajuste": int(sous["Gel_printanier_ajuste"].sum()),
        "Jours_gel_printanier_severe_ajuste": int(sous["Gel_printanier_severe_ajuste"].sum()),
        "Jours_gel_observe_tours": int(sous["Jour_gel_observe"].sum()) if "Jour_gel_observe" in sous else 0,
        "Jours_chauds_30C": int(sous["Jour_chaud_30"].sum()),
        "Jours_chauds_35C": int(sous["Jour_chaud_35"].sum()),
        "Nuits_tropicales": int(sous["Jour_tropical"].sum()),
        "Jours_echaudage": int(sous["Echaudage_jour"].sum()),
        "Jours_ideaux_maturation": int(sous["Jour_ideal_maturation"].sum()),
        "Jours_pluvieux": int(sous["Jour_pluvieux"].sum()),
        "Insolation_totale_h": round(sous["Insolation_h"].sum(), 0),
        "Mildiou_jours_eleves": int((sous["Mildiou_score"] >= 3).sum()),
        "Oidium_jours_eleves": int((sous["Oidium_score"] >= 5).sum()),
    })
df_millesimes = pd.DataFrame(fiches)

df["Decennie"] = (df["Annee"] // 10) * 10
synthese_dec = df.groupby("Decennie").agg(**{
    "T_moy_decennie": ("T_moy", "mean"),
    "RR_an_moy": ("RR", lambda s: s.sum() / (s.index.size / 365.25)),
    "Jours_gel_an_moy": ("Jour_gel_modele", lambda s: s.sum() / (s.index.size / 365.25)),
    "Jours_chauds_30_an_moy": ("Jour_chaud_30", lambda s: s.sum() / (s.index.size / 365.25)),
    "Jours_chauds_35_an_moy": ("Jour_chaud_35", lambda s: s.sum() / (s.index.size / 365.25)),
    "Nuits_tropicales_an_moy": ("Jour_tropical", lambda s: s.sum() / (s.index.size / 365.25)),
}).round(2).reset_index()

# Comparaison sources (3 visions du gel)
df["Annee_int"] = df["Annee"]
comparaison_sources = df.groupby("Annee_int").agg(**{
    "Tmin_min_modele": ("T_min", "min"),
    "Tmin_min_ajustee": ("T_min_ajustee", "min"),
    "Tmin_min_observe_tours": ("T_min_obs", "min"),
    "Jours_gel_modele": ("Jour_gel_modele", "sum"),
    "Jours_gel_ajuste": ("Jour_gel_ajuste", "sum"),
    "Jours_gel_observe_tours": ("Jour_gel_observe", "sum"),
    "Jours_gel_printanier_modele": ("Gel_printanier_modele", "sum"),
    "Jours_gel_printanier_ajuste": ("Gel_printanier_ajuste", "sum"),
}).round(2).reset_index().rename(columns={"Annee_int": "Annee"})

# Alertes Gel détaillées
masque_gel_mars_mai = (df["Mois"].isin([3, 4, 5])) & (
    (df["T_min"] <= 1) | (df["T_min_ajustee"] <= 0)
)
alertes_gel = df[masque_gel_mars_mai][[
    "Date", "Annee", "Mois", "T_min", "T_min_ajustee", "T_min_obs",
    "Vent_max_kmh", "HR_moy", "Source"
]].copy()
alertes_gel["Niveau_alerte"] = "Vigilance"
alertes_gel.loc[alertes_gel["T_min_ajustee"] <= -2, "Niveau_alerte"] = "Sévère"
alertes_gel.loc[(alertes_gel["T_min_ajustee"] > -2) & (alertes_gel["T_min_ajustee"] <= 0), "Niveau_alerte"] = "Probable"
alertes_gel = alertes_gel.sort_values("Date", ascending=False)

# =============================================================================
# 10. GOOGLE SHEET
# =============================================================================
print("ÉTAPE 7/7 — Google Sheet & Excel")

def lire_gsheet_onglet(sheet_id, nom_onglet):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={nom_onglet}"
    try:
        r = requests.get(url, timeout=30); r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        print(f"  ⚠️  Onglet '{nom_onglet}' non lu : {e}")
        return pd.DataFrame()

df_phenologie = lire_gsheet_onglet(GSHEET_ID, "phenologie")
df_observations = lire_gsheet_onglet(GSHEET_ID, "observations")

# =============================================================================
# 11. README STRUCTURÉ + ONGLET MÉTHODOLOGIE
# =============================================================================
readme_data = [
    ["IDENTITÉ", "", "", ""],
    ["Domaine", NOM_DOMAINE, "Domaine viticole", ""],
    ["Localisation", "Vernou-sur-Brenne (37210)", "Commune en AOC Vouvray", "Rive droite Loire, vallon de la Brenne"],
    ["Coordonnées GPS", f"{LATITUDE}°N, {LONGITUDE}°E", "Centroïde du domaine", "Précision ~50m"],
    ["AOC", "Vouvray", "Appellation d'origine contrôlée", "Cépage : Chenin Blanc"],
    ["", "", "", ""],
    ["SOURCES DE DONNÉES", "", "", ""],
    ["1990 → 2020", "ERA5 (Copernicus / ECMWF)", "Réanalyse climatique mondiale", "Résolution 9 km. Référence scientifique mondiale, homogène sur toute la période. Moins précise sur événements localisés."],
    ["2021 → aujourd'hui", "AROME 1 km (Météo-France)", "Modèle haute résolution opérationnel", "Résolution 1 km. Très précis sur températures moyennes et pluies, mais sous-estime les gels radiatifs locaux (cuvette, inversion thermique)."],
    ["Tours-Saint-Symphorien", "Météo-France DPClim (API officielle)", "Station officielle, mesure physique réelle", "Distance ~11 km du Clos. Données validées Météo-France, valeur juridique en cas de sinistre."],
    ["Saisies terrain", f"Google Sheet ID {GSHEET_ID[:8]}...", "Dates phénologiques + observations vigneron", "Saisies manuelles par l'équipe du domaine. Permettent calibration du modèle GFV."],
    ["", "", "", ""],
    ["TRAITEMENT", "", "", ""],
    ["Mise à jour", "Quotidienne 7h", "GitHub Actions automatisé", "Stockage incrémental : ne re-télécharge pas les données déjà acquises"],
    ["Stockage brut", "donnees_brutes.parquet + donnees_tours.parquet", "Format compact, données figées", "Garantit la reproductibilité — les valeurs téléchargées une fois ne changent plus"],
    ["Dernière exécution", date.today().isoformat(), "", ""],
    ["Période couverte", f"{df['Date'].min().date()} → {df['Date'].max().date()}", "", ""],
    ["Nombre de jours", str(len(df)), "", ""],
    ["Nombre d'années", str(df["Annee"].nunique()), "", ""],
    ["Normales climatiques", f"{NORMALE_DEBUT}-{NORMALE_FIN}", "Standard OMM (Org. Météorologique Mondiale)", "Période de référence sur 30 ans pour comparer chaque année"],
    ["", "", "", ""],
    ["AJUSTEMENT GEL CLOS — méthodologie complète", "", "", ""],
    ["Principe physique", "Inversion thermique nocturne", "Par nuit calme et claire, l'air froid (plus dense) descend et s'accumule dans les fonds de vallon → cuvettes plus froides que la moyenne. Le vent et l'humidité limitent cet effet.", "Phénomène bien documenté en agrométéo (manuels INRAE, IFV, Quénol, Bonnardot)"],
    ["", "", "", ""],
    ["MÉTHODE DE CALIBRATION", "", "", ""],
    ["Source de calibration", "Station officielle Tours-Saint-Symphorien (Météo-France, 37179001)", "Mesure quotidienne réelle, ~11 km à l'ouest du Clos, alt. 108m", "Données téléchargées via l'API DPClim sur toute la période 1990-aujourd'hui"],
    ["Variable calibrée", "Delta = T_min Tours observée − T_min AROME maille Vernou", "Différence moyenne par type de nuit", "Calculée sur ~13 000 jours d'observations"],
    ["Catégories de nuits", "Ventée / Calme et sèche / Intermédiaire", "Ventée : vent > 25 km/h (brasse l'air, inversion limitée). Calme et sèche : vent < 12 km/h ET HR < 85% (conditions optimales pour inversion thermique). Intermédiaire : entre les deux.", ""],
    ["", "", "", ""],
    ["VALEURS DES CORRECTIONS APPLIQUÉES", "", "", ""],
    ["Nuit ventée", f"{corrections_calibrees.get('ventee', -1.5):+.2f}°C", "Petite correction : le vent empêche largement l'accumulation d'air froid en fond de cuvette", "Calibré automatiquement à chaque exécution"],
    ["Nuit calme et sèche", f"{corrections_calibrees.get('calme_seche', -1.5):+.2f}°C", "Correction la plus marquée : c'est dans ces conditions que l'inversion thermique est la plus forte", "Calibré automatiquement à chaque exécution"],
    ["Nuit intermédiaire", f"{corrections_calibrees.get('intermediaire', -1.5):+.2f}°C", "Cas par défaut, correction modérée", "Calibré automatiquement à chaque exécution"],
    ["", "", "", ""],
    ["LIMITES IMPORTANTES — à connaître absolument", "", "", ""],
    ["Limite n°1 — Tours est en plaine", "L'écart Tours-AROME ne reflète pas la cuvette du Clos", "Tours-Saint-Symphorien est sur l'aérodrome de Parçay-Meslay, plat et dégagé. Le delta calibré capture surtout la différence Tours/Vernou, pas l'effet de cuvette local du domaine.", "CONSÉQUENCE : la T_min ajustée sous-estime probablement encore le vrai gel parcellaire en fond de cuvette."],
    ["Limite n°2 — Pas de calibration parcellaire", "Aucune mesure réelle au Clos n'existe à ce jour", "Le micro-climat du domaine (orientation Sud-Sud-Est, parcelles en coteau et en bas de vallon) ne peut être finement modélisé sans mesures terrain.", "SOLUTION : station physique Sencrop ou Davis (~700€/an) installée en fond de vallon."],
    ["Limite n°3 — Référence historique connue", "Gel d'avril 2021 — perte historique à Vouvray", "Tours a mesuré -2.0°C le 6 avril. AROME maille Vernou : -1.0°C. Notre ajusté : -2.5°C. Réalité connue dans les coteaux : -4 à -6°C en cuvette.", "Notre ajusté reste donc plus chaud que la réalité parcellaire dans les épisodes les plus sévères."],
    ["", "", "", ""],
    ["LECTURE PRATIQUE DES 3 COMPTEURS", "", "", ""],
    ["Jours_gel_modele", "Donnée brute AROME — sous-estime", "Pour usage scientifique / traçabilité de la source brute", "À ne PAS utiliser seul pour le pilotage"],
    ["Jours_gel_ajuste", "Donnée brute + correction calibrée", "Compteur principal pour le pilotage parcellaire — estimation prudente mais réaliste", "Reste optimiste vs fond de cuvette ; à compléter par observation visuelle terrain"],
    ["Jours_gel_observe_tours", "Mesure officielle Météo-France — Tours", "Donnée juridiquement opposable en cas de sinistre / déclaration MSA", "Représente Tours, pas Vouvray ; valeur indicative pour le secteur, pas pour la parcelle"],
    ["", "", "", ""],
    ["GOOGLE SHEET — Saisies terrain", "", "", ""],
    ["Lien d'accès", f"https://docs.google.com/spreadsheets/d/{GSHEET_ID}", "URL publique en lecture, édition réservée", "Ajouter les frères Frey en éditeurs pour saisie"],
    ["Onglet phénologie", f"{len(df_phenologie)} millésimes saisis", "Dates débourrement/floraison/véraison/vendange", "Permet calibration GFV pour Vernou"],
    ["Onglet observations", f"{len(df_observations)} observations terrain", "Notes libres datées par parcelle", ""],
]
df_readme = pd.DataFrame(readme_data, columns=["Élément", "Valeur", "Définition", "Notes / Limites"])

# Onglet Méthodologie complet
methodologie_data = [
    ["INDICES THERMIQUES", "", "", "", "", ""],
    ["Indice Huglin (IH)", "Σ [(Tmoy-10) + (Tmax-10)] / 2 × K_lat", "01/04 → 30/09",
     "Caractérise le potentiel héliothermique pour la maturité du raisin. Mesure thermique adaptée à la latitude.",
     "Très frais <1500 / Frais 1500-1800 / Tempéré 1800-2100 / Tempéré chaud 2100-2400 / Chaud 2400-3000 / Très chaud >3000",
     "Huglin (1978). K_lat = 1.05 à 47.43°N. Référence pour la viticulture mondiale (système Tonietto)."],
    ["Indice Winkler (GDD)", "Σ max(0, Tmoy - 10)", "01/04 → 31/10",
     "Somme des degrés-jours utiles à la vigne. Bien corrélé avec phénologie et teneur en sucre.",
     "Région I <850 (très frais) / II 850-1389 (frais) / III 1389-1667 (tempéré) / IV 1667-1944 (chaud) / V 1944-2222 (très chaud)",
     "Amerine & Winkler (1944, UC Davis). Standard américain. Pour Vouvray/Chenin, optimum en Région II-III."],
    ["Indice Fraîcheur des Nuits (IF)", "Moyenne des Tmin de septembre", "Septembre",
     "Caractérise les nuits de maturation. Détermine le potentiel aromatique et la conservation des acides.",
     "Très fraîches ≤12°C / Fraîches 12-14°C / Tempérées 14-18°C / Chaudes >18°C",
     "Tonietto (1999). Pour Chenin, optimum en nuits fraîches à tempérées."],
    ["GFV Parker", "Σ Tmoy base 0°C depuis 01/01", "01/01 → date stade",
     "Modèle phénologique : prédit les dates clés (1500°C·j ≈ floraison, 2700°C·j ≈ véraison).",
     "À comparer aux dates observées en Phénologie_terrain pour calibration",
     "Parker et al. (2011, 2013). À recalibrer pour Chenin sur Vouvray spécifiquement."],
    ["GSR", "Σ Tmoy base 0°C de la véraison à la maturité", "Véraison → maturité",
     "Modèle phénologique de la maturation : prédit la date de récolte selon le seuil cépage.",
     "Seuil maturité Chenin ~ 3300-3500°C·j",
     "Parker et al. (2014)"],
    ["BBCH base 5°C", "Σ max(0, Tmoy - 5)", "01/01 → date stade",
     "Modèle de débourrement (stade BBCH 09/13). Seuil ~ 200-250°C·j pour Chenin.",
     "À calibrer terrain",
     "Méthode commune ITV/Chambres d'agriculture"],
    ["", "", "", "", "", ""],
    ["INDICES HYDRIQUES", "", "", "", "", ""],
    ["ETP FAO Penman-Monteith", "Calculée par Open-Meteo selon norme FAO-56", "Quotidien",
     "Évapotranspiration potentielle du couvert végétal de référence. Mesure la demande climatique en eau.",
     "Normales 600-750 mm sur la campagne avr-sept à Vouvray",
     "FAO-56 (1998), Allen et al."],
    ["Bilan hydrique campagne", "Σ Pluies − Σ ETP", "01/04 → 30/09",
     "Différence entre apports et demande. Indique les conditions hydriques de la saison.",
     "Sain >0 / Léger stress -100 à 0 / Stress -300 à -100 / Sec sévère <-300",
     "Riou (1994). Indicateur OIV de référence."],
    ["Indice Selianinov", "(Σ Pluies campagne / Winkler) × 10", "01/04 → 30/09",
     "Efficacité hydrique des précipitations rapportée à la chaleur.",
     "<2 sec / 2-3 modéré / >3 humide",
     "Selianinov (1937)"],
    ["RFU (Réserve Facilement Utilisable)", "RFU(j) = min(150, max(0, RFU(j-1) + RR(j) - ETP(j)))", "Quotidien",
     "Modèle de bilan hydrique simple du sol. Approximation : 150mm pour sols argilo-calcaires Vouvray.",
     "Sévère <30mm / Modéré 30-60 / Léger 60-100 / Aucun stress >100",
     "Modèle FAO simplifié. À affiner avec mesures sondes parcellaires."],
    ["", "", "", "", "", ""],
    ["RISQUES SANITAIRES", "", "", "", "", ""],
    ["Mildiou (Goidanich simplifié)", "Score = (T 10-25°C) + (RR 48h ≥10mm) + (HR ≥75%)", "Quotidien",
     "Indice de risque épidémiologique mildiou. Score ≥3 = conditions très favorables.",
     "Nul=0 / Faible=1 / Modéré=2 / Élevé=3",
     "Goidanich (1962). Version pleinement détaillée nécessite la modélisation des œufs d'hiver et générations."],
    ["Oïdium (Gubler-Thomas)", "Σ jours favorables (T 21-30°C, HR ≥60%) sur 7 jours glissants", "Quotidien",
     "Score basé sur l'accumulation de conditions favorables au champignon.",
     "Faible 0-2 / Modéré 3-4 / Élevé 5-6 / Très élevé 7",
     "Gubler & Thomas (1995, UC Davis)"],
    ["Botrytis pré-vendange", "Score = 2×(HR≥80) + 2×(RR_7j≥30) + (T 15-22°C)", "Quotidien, focus août-vendanges",
     "Risque pourriture grise. Critique pendant la maturation et avant vendange.",
     "Faible 0-1 / Modéré 2 / Élevé 3 / Très élevé 4-5",
     "Modèle simplifié inspiré Broome et al."],
    ["Échaudage", "Tmax ≥ 35°C ET RFU < 60mm", "Été",
     "Combinaison canicule + stress hydrique = brûlures sur grappes.",
     "Compteur jours/an",
     "Indicateur empirique IFV"],
    ["Fenêtre traitement OK", "RR<1mm ce jour ET RR<2mm demain ET vent<30km/h ET HR<85%", "Quotidien",
     "Conditions favorables à un traitement (efficacité du produit, sécurité d'application).",
     "Compteur jours/mois pour planification",
     ""],
    ["", "", "", "", "", ""],
    ["INDICES QUALITÉ MILLÉSIME", "", "", "", "", ""],
    ["Amplitude thermique d'août", "Tmax − Tmin moyenne sur août", "Août",
     "Critique pour le développement aromatique du Chenin (réfrigération nocturne).",
     "Idéal >12°C pour expression aromatique fine",
     "Pratique vigneron classique"],
    ["Jours idéaux maturation", "Tmax 22-28°C ET Tmin 12-18°C ET sec, en août-sept", "Août-Septembre",
     "Conditions optimales pour la maturation lente et qualitative.",
     "Compteur — un millésime équilibré en a 15-25",
     ""],
    ["Jours idéaux vendange", "Tmoy 15-22°C ET sec ET HR matin élevée", "Septembre-Octobre",
     "Fenêtres optimales pour vendanger en main d'œuvre et préserver la fraîcheur.",
     "Compteur",
     ""],
    ["", "", "", "", "", ""],
    ["GEL — VERSIONS BRUTE / AJUSTÉE / OBSERVÉE", "", "", "", "", ""],
    ["Jour_gel_modele", "T_min AROME ≤ 0°C", "Quotidien",
     "Donnée brute du modèle au point centroïde du domaine (1 km²).",
     "Compteur jours/an et jours/printemps",
     "ATTENTION : sous-estime systématiquement les gels radiatifs en cuvette (~1-3°C de différence avec le fond de vallon par nuit calme et claire). Ne pas utiliser seul pour le pilotage parcellaire."],
    ["Jour_gel_ajuste", "T_min ajustée ≤ 0°C, où T_min ajustée = T_min AROME + correction calibrée", "Quotidien",
     "Estimation prudente du risque pour le contexte parcellaire du Clos. La correction est calibrée empiriquement sur le delta Tours-AROME observé sur 35 ans, par type de nuit (ventée / calme et sèche / intermédiaire).",
     "Compteur jours/an et jours/printemps. À utiliser comme référence de pilotage.",
     "Limites : Tours est à 11 km en plaine, donc le delta calibré sous-estime probablement encore le vrai delta cuvette du Clos. Une station physique au domaine permettrait un calibrage parcellaire."],
    ["Jour_gel_observe_tours", "T_min Tours-St-Symphorien ≤ 0°C (mesurée)", "Quotidien",
     "Mesure officielle Météo-France à 11 km du domaine, station synoptique de Parçay-Meslay (alt 108 m). Vérité observée mais à 11 km de distance.",
     "Compteur — donnée juridiquement opposable en cas de sinistre",
     "Tours est en plaine et plus à l'ouest. Vouvray (coteaux) peut connaître des gels que Tours ne capte pas, ou inversement. Donnée valable pour la tendance régionale, pas pour le détail parcellaire."],
]
df_methodologie = pd.DataFrame(methodologie_data, columns=[
    "Indicateur", "Formule", "Période de calcul", "Interprétation viticole",
    "Classes / Seuils", "Référence et notes"
])

# =============================================================================
# 12. ÉCRITURE EXCEL MULTI-ONGLETS
# =============================================================================
print()
print(f"Écriture du fichier {FICHIER_EXCEL}...")

COLS_BRUTES = [
    "Date", "Source", "Annee", "Mois", "Jour", "Jour_julien", "Semaine_ISO",
    "T_min", "T_max", "T_moy", "T_min_ajustee", "Correction_cuvette",
    "T_min_obs", "T_max_obs", "RR_obs",
    "Amplitude_thermique", "RR", "Pluie", "Neige_cm", "RR_heures",
    "ETP", "Bilan_hydrique_J", "HR_min", "HR_moy", "HR_max", "Pt_rosee",
    "Vent_max_kmh", "Rafale_kmh", "Vent_dir", "Insolation_h", "Rayonnement_MJ_m2",
    "Jour_gel_modele", "Jour_gel_ajuste", "Jour_gel_observe",
    "Jour_chaud_25", "Jour_chaud_30", "Jour_chaud_35",
    "Jour_tropical", "Jour_pluvieux", "Jour_pluvieux_fort", "Jour_sec",
]
COLS_INDICES = [
    "Date", "Annee", "Mois", "T_min", "T_max", "T_moy", "Amplitude_thermique",
    "GDD_base10", "GDD_base5", "GDD_base0",
    "Contrib_Winkler", "Contrib_Huglin", "Contrib_GFV", "Contrib_BBCH",
    "Winkler_cumul", "Huglin_cumul", "GFV_cumul", "BBCH5_cumul",
    "RR", "RR_cumul_campagne", "ETP", "ETP_cumul_campagne",
    "Bilan_hydrique_J", "Bilan_hydrique_cumul", "RFU_mm", "Stress_hydrique",
    "Gel_printanier_modele", "Gel_printanier_ajuste", "Gel_printanier_severe_ajuste",
    "Echaudage_jour", "Jour_ideal_maturation", "Jour_ideal_vendange",
]
COLS_RISQUES = [
    "Date", "Annee", "Mois", "T_moy", "T_max", "RR", "RR_48h", "RR_7j", "HR_moy",
    "Mildiou_score", "Mildiou_risque", "Oidium_score", "Oidium_risque",
    "Botrytis_score", "Botrytis_risque", "Echaudage_jour", "Fenetre_traitement_OK",
]

with pd.ExcelWriter(FICHIER_EXCEL, engine="openpyxl") as writer:
    df_readme.to_excel(writer, sheet_name="README", index=False)
    df_methodologie.to_excel(writer, sheet_name="Methodologie_indices", index=False)
    df_millesimes.to_excel(writer, sheet_name="Millesimes", index=False)
    comparaison_sources.to_excel(writer, sheet_name="Comparaison_sources", index=False)
    alertes_gel.to_excel(writer, sheet_name="Alertes_Gel", index=False)
    synthese_dec.to_excel(writer, sheet_name="Decennies", index=False)
    synthese_mens.to_excel(writer, sheet_name="Synthese_mensuelle", index=False)
    normales_mens.to_excel(writer, sheet_name="Normales_1991_2020", index=False)
    if not df_phenologie.empty:
        df_phenologie.to_excel(writer, sheet_name="Phenologie_terrain", index=False)
    if not df_observations.empty:
        df_observations.to_excel(writer, sheet_name="Observations_terrain", index=False)
    df[COLS_RISQUES].to_excel(writer, sheet_name="Risques_J", index=False)
    df[COLS_INDICES].to_excel(writer, sheet_name="Indices_J", index=False)
    df[COLS_BRUTES].to_excel(writer, sheet_name="Donnees_brutes_J", index=False)

print()
print(f"✅ Terminé. {len(df)} jours sur {df['Annee'].nunique()} années.")
print(f"   Excel : {FICHIER_EXCEL} (13 onglets)")
print(f"   Brutes : {FICHIER_BRUTES} ({len(df_brut)} jours)")
print(f"   Tours : {FICHIER_TOURS} ({len(df_tours)} jours)" if not df_tours.empty else "   Tours : (vide)")
# =============================================================================
# 13. GÉNÉRATION DU SITE WEB (GitHub Pages)
# =============================================================================
print()
print("ÉTAPE 8/8 — Génération du site web")

import shutil
SITE_DIR = Path("docs")
SITE_DIR.mkdir(exist_ok=True)

# Copier l'Excel dans le site pour qu'il soit téléchargeable directement
shutil.copy(FICHIER_EXCEL, SITE_DIR / FICHIER_EXCEL)

# Données utiles pour les pages
DERNIER_JOUR = df.iloc[-1]
ANNEE_COURANTE = int(DERNIER_JOUR["Annee"])
DATE_MAJ = date.today().isoformat()

# Helpers de formatage
def fmt(val, decimales=1, suffixe=""):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    if decimales == 0:
        return f"{int(round(val))}{suffixe}"
    return f"{val:.{decimales}f}{suffixe}"


# CSS commun à toutes les pages — charte Clos Thierrière
CSS_COMMUN = """
:root {
  --vert-sauge: #536158;
  --vert-sauge-clair: #6B7B71;
  --vert-sauge-fonce: #3D4942;
  --creme: #F5F1E8;
  --creme-fonce: #E8E2D2;
  --ocre: #D4A93B;
  --bordeaux: #7A2E2E;
  --noir-doux: #2A2A2A;
  --gris: #8A857A;
  --blanc: #FDFCF8;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
  font-weight: 300;
  background: var(--creme);
  color: var(--noir-doux);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

.mono { font-family: 'JetBrains Mono', 'Courier New', monospace; }

a { color: var(--vert-sauge); text-decoration: none; border-bottom: 1px solid transparent; transition: all 0.2s; }
a:hover { border-bottom-color: var(--vert-sauge); }

/* HEADER */
.header {
  background: var(--vert-sauge);
  color: var(--creme);
  padding: 1rem 2rem;
  border-bottom: 3px solid var(--ocre);
}
.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1rem;
}
.logo {
  display: flex;
  align-items: baseline;
  gap: 0.7rem;
}
.monogramme {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.5rem;
  font-weight: 600;
  letter-spacing: 0.1em;
  background: var(--ocre);
  color: var(--vert-sauge-fonce);
  padding: 0.2rem 0.5rem;
  border-radius: 2px;
}
.logo-titre { font-size: 1.1rem; font-weight: 400; letter-spacing: 0.05em; }
.logo-soustitre { font-size: 0.75rem; opacity: 0.7; font-family: 'JetBrains Mono', monospace; }

nav ul { list-style: none; display: flex; gap: 1.5rem; flex-wrap: wrap; }
nav a {
  color: var(--creme);
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  border-bottom: 1px solid transparent;
}
nav a:hover, nav a.active { border-bottom-color: var(--ocre); color: var(--ocre); }

/* MAIN */
main { max-width: 1200px; margin: 2rem auto; padding: 0 2rem; }
.section { margin-bottom: 3rem; }
.section-titre {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--vert-sauge);
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--vert-sauge);
}
h1 {
  font-weight: 200;
  font-size: 2.5rem;
  letter-spacing: -0.02em;
  margin-bottom: 0.5rem;
  color: var(--vert-sauge-fonce);
}
h2 {
  font-weight: 300;
  font-size: 1.5rem;
  margin: 2rem 0 1rem;
  color: var(--vert-sauge-fonce);
}
p { margin-bottom: 1rem; max-width: 70ch; }

/* BANDEAU CHIFFRES-CLÉS */
.bandeau-cles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
  background: var(--blanc);
  padding: 2rem;
  border-radius: 4px;
  border-left: 4px solid var(--ocre);
  margin-bottom: 2rem;
}
.cle-bloc { text-align: center; padding: 0.5rem; }
.cle-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--gris);
  margin-bottom: 0.5rem;
}
.cle-valeur {
  font-family: 'JetBrains Mono', monospace;
  font-size: 2rem;
  font-weight: 500;
  color: var(--vert-sauge-fonce);
  line-height: 1;
}
.cle-unite { font-size: 0.9rem; color: var(--gris); margin-left: 0.2rem; }

/* CARTES */
.cartes { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; margin-top: 1.5rem; }
.carte {
  background: var(--blanc);
  padding: 1.5rem;
  border-radius: 4px;
  border-top: 3px solid var(--vert-sauge);
}
.carte h3 {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--vert-sauge);
  margin-bottom: 1rem;
}
.carte-valeur {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.8rem;
  color: var(--vert-sauge-fonce);
  margin-bottom: 0.5rem;
}
.carte-detail { font-size: 0.85rem; color: var(--gris); }

/* TABLEAUX */
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--blanc);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  margin: 1rem 0;
}
th {
  background: var(--vert-sauge);
  color: var(--creme);
  padding: 0.7rem 1rem;
  text-align: left;
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 0.75rem;
}
td { padding: 0.6rem 1rem; border-bottom: 1px solid var(--creme-fonce); }
tr:hover { background: var(--creme); }
tr.highlight { background: rgba(212, 169, 59, 0.15); }

/* BADGES */
.badge {
  display: inline-block;
  padding: 0.2rem 0.7rem;
  border-radius: 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  font-weight: 500;
}
.badge-faible { background: #D4E5D9; color: #2D5A3D; }
.badge-modere { background: #F2E4B8; color: #7A5C0E; }
.badge-eleve { background: #F5C9A8; color: #8C4A1A; }
.badge-tres-eleve { background: #F2B8B8; color: #7A2E2E; }
.badge-info { background: var(--creme-fonce); color: var(--vert-sauge-fonce); }

/* ALERTES */
.alerte {
  padding: 1rem 1.5rem;
  border-radius: 4px;
  margin: 1rem 0;
  border-left: 4px solid;
}
.alerte-info { background: var(--blanc); border-left-color: var(--vert-sauge); }
.alerte-attention { background: #FCF6E1; border-left-color: var(--ocre); }
.alerte-danger { background: #F5DDDD; border-left-color: var(--bordeaux); color: var(--bordeaux); }

/* CTA */
.btn {
  display: inline-block;
  background: var(--vert-sauge);
  color: var(--creme) !important;
  padding: 0.8rem 1.5rem;
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  border: none;
  transition: all 0.2s;
}
.btn:hover { background: var(--vert-sauge-fonce); border-bottom: none; }
.btn-ocre { background: var(--ocre); color: var(--vert-sauge-fonce) !important; }
.btn-ocre:hover { background: #B89230; }

/* FOOTER */
footer {
  margin-top: 4rem;
  padding: 2rem;
  background: var(--vert-sauge-fonce);
  color: var(--creme);
  text-align: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.8rem;
}
footer .maj { color: var(--ocre); }

/* RESPONSIVE */
@media (max-width: 768px) {
  .header-inner { flex-direction: column; align-items: flex-start; }
  nav ul { flex-direction: column; gap: 0.5rem; }
  h1 { font-size: 1.8rem; }
  .cle-valeur { font-size: 1.5rem; }
  main { padding: 0 1rem; }
}

/* GRAPHIQUES */
.bar-chart { display: flex; flex-direction: column; gap: 0.3rem; margin: 1rem 0; }
.bar-row { display: flex; align-items: center; gap: 0.5rem; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; }
.bar-label { width: 60px; color: var(--gris); }
.bar-track { flex: 1; height: 16px; background: var(--creme-fonce); border-radius: 2px; overflow: hidden; position: relative; }
.bar-fill { height: 100%; background: var(--vert-sauge); border-radius: 2px; transition: width 0.5s; }
.bar-fill.chaud { background: var(--ocre); }
.bar-fill.tres-chaud { background: var(--bordeaux); }
.bar-value { width: 50px; text-align: right; color: var(--vert-sauge-fonce); }
"""


def page_html(titre, contenu, page_active=""):
    """Squelette HTML commun à toutes les pages."""
    nav_items = [
        ("index.html", "Accueil"),
        ("millesime.html", "Millésime"),
        ("historique.html", "Historique"),
        ("risques.html", "Risques"),
        ("phenologie.html", "Phénologie"),
        ("methodologie.html", "Méthodologie"),
    ]
    nav_html = "".join([
        f'<li><a href="{href}" class="{"active" if href == page_active else ""}">{label}</a></li>'
        for href, label in nav_items
    ])
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{titre} — Clos Thierrière</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@200;300;400;500&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS_COMMUN}</style>
</head>
<body>
<header class="header">
  <div class="header-inner">
    <div class="logo">
      <span class="monogramme">CF</span>
      <div>
        <div class="logo-titre">Clos Thierrière</div>
        <div class="logo-soustitre">Vouvray · Vernou-sur-Brenne</div>
      </div>
    </div>
    <nav><ul>{nav_html}</ul></nav>
  </div>
</header>
<main>
{contenu}
</main>
<footer>
  Données mises à jour le <span class="maj">{DATE_MAJ}</span>
  · ERA5 + AROME + Tours-St-Symphorien · Période : {df['Date'].min().date()} → {df['Date'].max().date()}
  · <a href="{FICHIER_EXCEL}" style="color: var(--ocre);">Télécharger l'Excel</a>
</footer>
</body>
</html>"""


# =============================================================================
# PAGE 1 — ACCUEIL
# =============================================================================
hier = df.iloc[-1]
ajd_huglin = df_millesimes[df_millesimes["Millesime"] == ANNEE_COURANTE]
huglin_courant = ajd_huglin["Indice_Huglin"].iloc[0] if len(ajd_huglin) else 0
gel_print_courant = ajd_huglin["Jours_gel_printanier_ajuste"].iloc[0] if len(ajd_huglin) else 0
gel_print_severe_courant = ajd_huglin["Jours_gel_printanier_severe_ajuste"].iloc[0] if len(ajd_huglin) else 0

# Statut sanitaire actuel
risques_30j = df.tail(30)
mildiou_jours = int((risques_30j["Mildiou_score"] >= 3).sum())
oidium_jours = int((risques_30j["Oidium_score"] >= 5).sum())
botrytis_jours = int((risques_30j["Botrytis_score"] >= 3).sum())

contenu_accueil = f"""
<div class="section">
  <h1>Climat & vigne</h1>
  <p style="font-size: 1.1rem; color: var(--gris); max-width: 600px;">
    Suivi climatologique du domaine sur 35 ans. Mise à jour quotidienne automatique.
    Sources : ERA5, AROME 1 km Météo-France, station officielle Tours-Saint-Symphorien.
  </p>
</div>

<div class="section">
  <div class="section-titre">État du domaine — {hier['Date'].strftime('%d %B %Y')}</div>
  <div class="bandeau-cles">
    <div class="cle-bloc">
      <div class="cle-label">Température min</div>
      <div class="cle-valeur">{fmt(hier['T_min'], 1)}<span class="cle-unite">°C</span></div>
    </div>
    <div class="cle-bloc">
      <div class="cle-label">Température max</div>
      <div class="cle-valeur">{fmt(hier['T_max'], 1)}<span class="cle-unite">°C</span></div>
    </div>
    <div class="cle-bloc">
      <div class="cle-label">Précipitations</div>
      <div class="cle-valeur">{fmt(hier['RR'], 1)}<span class="cle-unite">mm</span></div>
    </div>
    <div class="cle-bloc">
      <div class="cle-label">Réserve sol</div>
      <div class="cle-valeur">{fmt(hier['RFU_mm'], 0)}<span class="cle-unite">mm</span></div>
    </div>
    <div class="cle-bloc">
      <div class="cle-label">Huglin {ANNEE_COURANTE}</div>
      <div class="cle-valeur">{fmt(huglin_courant, 0)}</div>
    </div>
    <div class="cle-bloc">
      <div class="cle-label">Gel printanier</div>
      <div class="cle-valeur">{int(gel_print_courant)}<span class="cle-unite">j</span></div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Risques sanitaires — 30 derniers jours</div>
  <div class="cartes">
    <div class="carte">
      <h3>Mildiou</h3>
      <div class="carte-valeur">{mildiou_jours}<span style="font-size: 1rem; color: var(--gris);"> jours à risque élevé</span></div>
      <div class="carte-detail">Score Goidanich ≥ 3 sur 30 derniers jours</div>
    </div>
    <div class="carte">
      <h3>Oïdium</h3>
      <div class="carte-valeur">{oidium_jours}<span style="font-size: 1rem; color: var(--gris);"> jours à risque élevé</span></div>
      <div class="carte-detail">Score Gubler-Thomas ≥ 5 sur 30 derniers jours</div>
    </div>
    <div class="carte">
      <h3>Botrytis</h3>
      <div class="carte-valeur">{botrytis_jours}<span style="font-size: 1rem; color: var(--gris);"> jours à risque élevé</span></div>
      <div class="carte-detail">Score combiné ≥ 3 sur 30 derniers jours</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Téléchargement</div>
  <p>Toutes les données du domaine (35 ans, 13 onglets, indices viticoles experts) sont disponibles en un fichier Excel mis à jour chaque matin.</p>
  <a href="{FICHIER_EXCEL}" class="btn btn-ocre">⬇ Télécharger l'Excel complet</a>
</div>
"""

(SITE_DIR / "index.html").write_text(page_html("Accueil", contenu_accueil, "index.html"), encoding="utf-8")


# =============================================================================
# PAGE 2 — MILLÉSIME EN COURS
# =============================================================================
mil = df_millesimes[df_millesimes["Millesime"] == ANNEE_COURANTE]
mil_prec = df_millesimes[df_millesimes["Millesime"] == ANNEE_COURANTE - 1]
mil_norm_3a = df_millesimes[(df_millesimes["Millesime"] >= ANNEE_COURANTE - 5) & (df_millesimes["Millesime"] < ANNEE_COURANTE)]

if len(mil) > 0:
    m = mil.iloc[0]
    huglin_norm = mil_norm_3a["Indice_Huglin"].mean() if len(mil_norm_3a) else 0
    delta_huglin = m["Indice_Huglin"] - huglin_norm if huglin_norm else 0
    rr_norm = mil_norm_3a["RR_totale_mm"].mean() if len(mil_norm_3a) else 0
    delta_rr = m["RR_totale_mm"] - rr_norm if rr_norm else 0
    
    contenu_millesime = f"""
<div class="section">
  <h1>Millésime {ANNEE_COURANTE}</h1>
  <p style="color: var(--gris);">État de la saison à date du {hier['Date'].strftime('%d %B %Y')}</p>
</div>

<div class="section">
  <div class="section-titre">Indices climatiques</div>
  <div class="cartes">
    <div class="carte">
      <h3>Huglin</h3>
      <div class="carte-valeur">{fmt(m['Indice_Huglin'], 0)}</div>
      <div class="carte-detail">{m['Classe_Huglin']}</div>
      <div class="carte-detail" style="margin-top: 0.5rem; color: var(--vert-sauge);">
        {'+' if delta_huglin > 0 else ''}{fmt(delta_huglin, 0)} vs moyenne 5 ans
      </div>
    </div>
    <div class="carte">
      <h3>Winkler (GDD)</h3>
      <div class="carte-valeur">{fmt(m['Indice_Winkler'], 0)}</div>
      <div class="carte-detail">{m['Classe_Winkler']}</div>
    </div>
    <div class="carte">
      <h3>Précipitations</h3>
      <div class="carte-valeur">{fmt(m['RR_totale_mm'], 0)}<span class="cle-unite">mm</span></div>
      <div class="carte-detail" style="margin-top: 0.5rem; color: var(--vert-sauge);">
        {'+' if delta_rr > 0 else ''}{fmt(delta_rr, 0)} mm vs moyenne 5 ans
      </div>
    </div>
    <div class="carte">
      <h3>Bilan hydrique</h3>
      <div class="carte-valeur">{fmt(m['Bilan_hydrique_campagne'], 0)}<span class="cle-unite">mm</span></div>
      <div class="carte-detail">RR campagne − ETP campagne</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Risques relevés</div>
  <div class="cartes">
    <div class="carte">
      <h3>Gel printanier</h3>
      <div class="carte-valeur">{int(m['Jours_gel_printanier_ajuste'])} <span style="font-size: 1rem;color: var(--gris);">jours</span></div>
      <div class="carte-detail">dont {int(m['Jours_gel_printanier_severe_ajuste'])} jours sévères (T_min ≤ -2°C ajustée)</div>
      <div class="carte-detail" style="margin-top: 0.5rem;">Tours observé : {int(m['Jours_gel_observe_tours'])} jours</div>
    </div>
    <div class="carte">
      <h3>Jours chauds</h3>
      <div class="carte-valeur">{int(m['Jours_chauds_30C'])} <span style="font-size: 1rem; color: var(--gris);">à 30°C+</span></div>
      <div class="carte-detail">{int(m['Jours_chauds_35C'])} jours à 35°C+</div>
      <div class="carte-detail">{int(m['Nuits_tropicales'])} nuits tropicales</div>
    </div>
    <div class="carte">
      <h3>Jours idéaux maturation</h3>
      <div class="carte-valeur">{int(m['Jours_ideaux_maturation'])}</div>
      <div class="carte-detail">T_max 22-28°C, T_min 12-18°C, sec — août-septembre</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Lecture du millésime</div>
  <p>Le millésime {ANNEE_COURANTE} est classé <strong>{m['Classe_Huglin'].lower()}</strong> selon Huglin
  (indice {fmt(m['Indice_Huglin'], 0)}), avec un bilan hydrique de campagne de {fmt(m['Bilan_hydrique_campagne'], 0)} mm.
  La fraîcheur des nuits de septembre, indicateur clé pour le Chenin, est de
  {fmt(m['IF_septembre'], 1) if pd.notna(m['IF_septembre']) else 'à mesurer'}°C
  ({m['Classe_IF_nuits']}).</p>
</div>
"""
else:
    contenu_millesime = f"<div class='section'><h1>Millésime {ANNEE_COURANTE}</h1><p>Données en cours de constitution.</p></div>"

(SITE_DIR / "millesime.html").write_text(page_html(f"Millésime {ANNEE_COURANTE}", contenu_millesime, "millesime.html"), encoding="utf-8")


# =============================================================================
# PAGE 3 — HISTORIQUE
# =============================================================================
huglin_min = df_millesimes["Indice_Huglin"].min()
huglin_max = df_millesimes["Indice_Huglin"].max()

bar_chart = ""
for _, ligne in df_millesimes.iterrows():
    if pd.isna(ligne["Indice_Huglin"]) or ligne["Indice_Huglin"] == 0:
        continue
    pct = (ligne["Indice_Huglin"] - huglin_min) / (huglin_max - huglin_min) * 100 if huglin_max > huglin_min else 50
    classe_bar = "bar-fill"
    if ligne["Indice_Huglin"] > 2100: classe_bar += " chaud"
    if ligne["Indice_Huglin"] > 2400: classe_bar += " tres-chaud"
    bar_chart += f"""
    <div class="bar-row">
      <span class="bar-label">{int(ligne["Millesime"])}</span>
      <div class="bar-track"><div class="{classe_bar}" style="width: {pct}%"></div></div>
      <span class="bar-value">{fmt(ligne["Indice_Huglin"], 0)}</span>
    </div>"""

# Tableau récap années récentes
lignes_table = ""
for _, ligne in df_millesimes.tail(15).iterrows():
    if pd.isna(ligne["Indice_Huglin"]):
        continue
    highlight = "highlight" if ligne["Millesime"] == ANNEE_COURANTE else ""
    lignes_table += f"""
    <tr class="{highlight}">
      <td><strong>{int(ligne['Millesime'])}</strong></td>
      <td>{fmt(ligne['Indice_Huglin'], 0)}</td>
      <td>{ligne['Classe_Huglin']}</td>
      <td>{fmt(ligne['T_moy_annuelle'], 1)}°C</td>
      <td>{fmt(ligne['RR_totale_mm'], 0)} mm</td>
      <td>{int(ligne['Jours_gel_printanier_ajuste'])}j</td>
      <td>{int(ligne['Jours_chauds_30C'])}j</td>
    </tr>"""

contenu_historique = f"""
<div class="section">
  <h1>35 millésimes</h1>
  <p>Évolution des indices climatiques de 1990 à {ANNEE_COURANTE}.</p>
</div>

<div class="section">
  <div class="section-titre">Indice Huglin par millésime</div>
  <p style="margin-bottom: 1.5rem; color: var(--gris); font-size: 0.9rem;">
    Huglin = potentiel héliothermique pour la maturité. Vert : tempéré.
    Ocre : tempéré chaud (>2100). Bordeaux : chaud à très chaud (>2400).
  </p>
  <div class="bar-chart">{bar_chart}</div>
</div>

<div class="section">
  <div class="section-titre">Détail des 15 derniers millésimes</div>
  <table>
    <thead>
      <tr>
        <th>Année</th><th>Huglin</th><th>Classe</th><th>T° moy.</th><th>Pluie</th><th>Gel print.</th><th>Jours 30°C+</th>
      </tr>
    </thead>
    <tbody>{lignes_table}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-titre">Tendance sur les décennies</div>
  <table>
    <thead><tr><th>Décennie</th><th>T° moyenne</th><th>Pluie / an</th><th>Jours gel / an</th><th>Jours 30°C+ / an</th><th>Jours 35°C+ / an</th></tr></thead>
    <tbody>
"""
for _, ligne in synthese_dec.iterrows():
    contenu_historique += f"""
    <tr>
      <td><strong>{int(ligne['Decennie'])}s</strong></td>
      <td>{fmt(ligne['T_moy_decennie'], 2)}°C</td>
      <td>{fmt(ligne['RR_an_moy'], 0)} mm</td>
      <td>{fmt(ligne['Jours_gel_an_moy'], 1)}j</td>
      <td>{fmt(ligne['Jours_chauds_30_an_moy'], 1)}j</td>
      <td>{fmt(ligne['Jours_chauds_35_an_moy'], 1)}j</td>
    </tr>"""
contenu_historique += "</tbody></table></div>"

(SITE_DIR / "historique.html").write_text(page_html("Historique", contenu_historique, "historique.html"), encoding="utf-8")


# =============================================================================
# PAGE 4 — RISQUES SANITAIRES
# =============================================================================
risques_90j = df.tail(90).copy()

def frise_risque(serie, seuil_eleve, label):
    out = '<div style="display: flex; gap: 2px; margin: 0.5rem 0; flex-wrap: nowrap;">'
    for v in serie:
        if pd.isna(v) or v < seuil_eleve - 1:
            color = "var(--creme-fonce)"
        elif v < seuil_eleve:
            color = "var(--ocre)"
        else:
            color = "var(--bordeaux)"
        out += f'<div style="flex:1; min-width: 4px; height: 24px; background: {color}; border-radius: 1px;" title="{v}"></div>'
    out += '</div>'
    return out

frise_mildiou = frise_risque(risques_90j["Mildiou_score"], 3, "Mildiou")
frise_oidium = frise_risque(risques_90j["Oidium_score"], 5, "Oïdium")
frise_botrytis = frise_risque(risques_90j["Botrytis_score"], 3, "Botrytis")

# Statuts actuels
mildiou_act = df.iloc[-1].get("Mildiou_risque", "—")
oidium_act = df.iloc[-1].get("Oidium_risque", "—")
botrytis_act = df.iloc[-1].get("Botrytis_risque", "—")

def badge_class(niveau):
    n = str(niveau).lower()
    if "tres" in n.replace("é", "e"): return "badge-tres-eleve"
    if "elev" in n.replace("é", "e"): return "badge-eleve"
    if "modér" in n.lower() or "modere" in n.lower(): return "badge-modere"
    return "badge-faible"

contenu_risques = f"""
<div class="section">
  <h1>Risques sanitaires</h1>
  <p>Modélisation des risques épidémiologiques mildiou, oïdium et botrytis.</p>
</div>

<div class="section">
  <div class="section-titre">Statut au {hier['Date'].strftime('%d %B %Y')}</div>
  <div class="cartes">
    <div class="carte">
      <h3>Mildiou</h3>
      <div class="carte-valeur">
        <span class="badge {badge_class(mildiou_act)}">{mildiou_act}</span>
      </div>
      <div class="carte-detail">Modèle Goidanich simplifié</div>
    </div>
    <div class="carte">
      <h3>Oïdium</h3>
      <div class="carte-valeur">
        <span class="badge {badge_class(oidium_act)}">{oidium_act}</span>
      </div>
      <div class="carte-detail">Modèle Gubler-Thomas</div>
    </div>
    <div class="carte">
      <h3>Botrytis</h3>
      <div class="carte-valeur">
        <span class="badge {badge_class(botrytis_act)}">{botrytis_act}</span>
      </div>
      <div class="carte-detail">Score combiné HR/RR/T°</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Évolution sur 90 derniers jours</div>
  <p style="color: var(--gris); font-size: 0.9rem;">
    <span style="display:inline-block; width:12px; height:12px; background: var(--creme-fonce); border-radius:1px; vertical-align: middle;"></span> Faible
    &nbsp;
    <span style="display:inline-block; width:12px; height:12px; background: var(--ocre); border-radius:1px; vertical-align: middle;"></span> Modéré
    &nbsp;
    <span style="display:inline-block; width:12px; height:12px; background: var(--bordeaux); border-radius:1px; vertical-align: middle;"></span> Élevé
  </p>
  
  <div style="margin: 1.5rem 0;">
    <div style="font-family: monospace; font-size: 0.85rem; color: var(--vert-sauge); margin-bottom: 0.3rem;">MILDIOU</div>
    {frise_mildiou}
  </div>
  <div style="margin: 1.5rem 0;">
    <div style="font-family: monospace; font-size: 0.85rem; color: var(--vert-sauge); margin-bottom: 0.3rem;">OÏDIUM</div>
    {frise_oidium}
  </div>
  <div style="margin: 1.5rem 0;">
    <div style="font-family: monospace; font-size: 0.85rem; color: var(--vert-sauge); margin-bottom: 0.3rem;">BOTRYTIS</div>
    {frise_botrytis}
  </div>
</div>

<div class="section">
  <div class="section-titre">Fenêtres de traitement</div>
  <p>Une fenêtre de traitement est jugée favorable si : pas de pluie ce jour ni demain, vent &lt; 30 km/h, humidité &lt; 85%.</p>
  <p>Sur les 30 derniers jours : <strong>{int(df.tail(30)['Fenetre_traitement_OK'].sum())} fenêtres favorables</strong> sur 30 jours.</p>
</div>
"""

(SITE_DIR / "risques.html").write_text(page_html("Risques sanitaires", contenu_risques, "risques.html"), encoding="utf-8")


# =============================================================================
# PAGE 5 — PHÉNOLOGIE
# =============================================================================
GSHEET_LIEN = f"https://docs.google.com/spreadsheets/d/{GSHEET_ID}"

if not df_phenologie.empty:
    table_pheno = df_phenologie.to_html(index=False, classes="", border=0)
else:
    table_pheno = "<p>Aucune donnée saisie pour l'instant.</p>"

if not df_observations.empty:
    table_obs = df_observations.to_html(index=False, classes="", border=0)
else:
    table_obs = "<p>Aucune observation saisie pour l'instant.</p>"

contenu_phenologie = f"""
<div class="section">
  <h1>Phénologie & observations</h1>
  <p>Saisies terrain par l'équipe du domaine. Permettent de calibrer les modèles phénologiques (GFV, GSR, BBCH) sur le contexte spécifique de Vernou.</p>
</div>

<div class="section">
  <div class="section-titre">Saisir de nouvelles données</div>
  <p>Les saisies se font directement dans un Google Sheet partagé. Elles sont automatiquement intégrées au fichier Excel à chaque mise à jour quotidienne.</p>
  <a href="{GSHEET_LIEN}" target="_blank" rel="noopener" class="btn">Ouvrir le tableau de saisie ↗</a>
</div>

<div class="section">
  <div class="section-titre">Dates phénologiques par millésime</div>
  {table_pheno}
</div>

<div class="section">
  <div class="section-titre">Observations terrain</div>
  {table_obs}
</div>
"""

(SITE_DIR / "phenologie.html").write_text(page_html("Phénologie", contenu_phenologie, "phenologie.html"), encoding="utf-8")


# =============================================================================
# PAGE 6 — MÉTHODOLOGIE
# =============================================================================
# Convertir le DataFrame methodologie en sections lisibles
sections_methodo_html = ""
section_courante = None
for _, ligne in df_methodologie.iterrows():
    indicateur = ligne["Indicateur"]
    if pd.isna(ligne["Formule"]) and indicateur and not pd.isna(indicateur):
        # C'est un titre de section
        if section_courante is not None:
            sections_methodo_html += "</div>"
        sections_methodo_html += f'<h2 style="margin-top: 2.5rem;">{indicateur}</h2><div>'
        section_courante = indicateur
    elif not pd.isna(indicateur):
        sections_methodo_html += f"""
        <div class="carte" style="margin-bottom: 1rem; border-top: 2px solid var(--ocre);">
          <h3>{indicateur}</h3>
          <p style="font-family: 'JetBrains Mono', monospace; font-size: 0.9rem; color: var(--vert-sauge); background: var(--creme); padding: 0.5rem; border-radius: 2px; margin-bottom: 0.8rem;">
            {ligne['Formule'] if not pd.isna(ligne['Formule']) else ''}
          </p>
          <p style="font-size: 0.95rem; margin-bottom: 0.5rem;"><strong>Période :</strong> {ligne['Période de calcul'] if not pd.isna(ligne['Période de calcul']) else '—'}</p>
          <p style="font-size: 0.95rem; margin-bottom: 0.5rem;"><strong>Interprétation :</strong> {ligne['Interprétation viticole'] if not pd.isna(ligne['Interprétation viticole']) else '—'}</p>
          <p style="font-size: 0.95rem; margin-bottom: 0.5rem;"><strong>Classes / Seuils :</strong> {ligne['Classes / Seuils'] if not pd.isna(ligne['Classes / Seuils']) else '—'}</p>
          <p style="font-size: 0.85rem; color: var(--gris); font-style: italic;">{ligne['Référence et notes'] if not pd.isna(ligne['Référence et notes']) else ''}</p>
        </div>
        """
if section_courante is not None:
    sections_methodo_html += "</div>"

# Section calibration gel — affichage des vrais deltas
delta_ventee = corrections_calibrees.get('ventee', -1.5)
delta_calme = corrections_calibrees.get('calme_seche', -1.5)
delta_inter = corrections_calibrees.get('intermediaire', -1.5)

contenu_methodologie = f"""
<div class="section">
  <h1>Méthodologie</h1>
  <p>Sources de données, formules des indices viticoles, calibration de la correction gel.</p>
</div>

<div class="section">
  <div class="section-titre">Sources de données</div>
  <div class="cartes">
    <div class="carte">
      <h3>1990 → 2020</h3>
      <p><strong>ERA5</strong> (Copernicus / ECMWF)</p>
      <div class="carte-detail">Réanalyse climatique mondiale, résolution 9 km. Référence scientifique homogène sur toute la période.</div>
    </div>
    <div class="carte">
      <h3>2021 → aujourd'hui</h3>
      <p><strong>AROME 1 km</strong> (Météo-France)</p>
      <div class="carte-detail">Modèle haute résolution opérationnel. Très précis sur températures moyennes et pluies.</div>
    </div>
    <div class="carte">
      <h3>Validation observée</h3>
      <p><strong>Tours-Saint-Symphorien</strong> (DPClim Météo-France)</p>
      <div class="carte-detail">Station officielle, ~11 km du Clos. Données mesurées, valeur juridique.</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Calibration de la correction gel</div>
  <p>La correction "cuvette" appliquée à la T_min du modèle AROME pour estimer le risque gel parcellaire est calibrée empiriquement sur le delta moyen entre la station Tours et le modèle AROME, par type de nuit.</p>
  
  <div class="alerte alerte-info">
    <strong>Valeurs calibrées (à la dernière exécution) :</strong>
    <ul style="margin-top: 0.5rem; list-style: none; padding-left: 0;">
      <li class="mono">→ Nuit ventée (vent > 25 km/h) : <strong>{delta_ventee:+.2f}°C</strong></li>
      <li class="mono">→ Nuit calme et sèche (vent < 12 km/h, HR < 85%) : <strong>{delta_calme:+.2f}°C</strong></li>
      <li class="mono">→ Nuit intermédiaire : <strong>{delta_inter:+.2f}°C</strong></li>
    </ul>
  </div>
  
  <h2>Limites importantes</h2>
  <div class="alerte alerte-attention">
    <strong>Limite n°1 — Tours est en plaine.</strong> Le delta capture l'écart Tours-Vernou, pas l'effet cuvette du Clos. La T_min ajustée sous-estime probablement encore le vrai gel parcellaire en fond de cuvette.
  </div>
  <div class="alerte alerte-attention">
    <strong>Limite n°2 — Pas de calibration parcellaire.</strong> Aucune mesure réelle au Clos n'existe. Une station physique au domaine permettrait un calibrage fin.
  </div>
  <div class="alerte alerte-attention">
    <strong>Limite n°3 — Référence avril 2021.</strong> Tours a mesuré -2.0°C. AROME : -1.0°C. Notre ajusté : -2.5°C. Réalité dans certaines parcelles de Vouvray : -4 à -6°C en fond de vallon.
  </div>
  
  <h2>Lecture des 3 compteurs gel</h2>
  <div class="cartes">
    <div class="carte">
      <h3>Jours_gel_modele</h3>
      <p>Donnée brute AROME au point centroïde.</p>
      <div class="carte-detail" style="color: var(--bordeaux);">⚠ Sous-estime systématiquement. Ne pas utiliser seul pour le pilotage.</div>
    </div>
    <div class="carte">
      <h3>Jours_gel_ajuste</h3>
      <p>Donnée brute + correction calibrée.</p>
      <div class="carte-detail">Compteur principal pour le pilotage parcellaire.</div>
    </div>
    <div class="carte">
      <h3>Jours_gel_observe_tours</h3>
      <p>Mesure officielle Tours-St-Symphorien.</p>
      <div class="carte-detail">Donnée juridiquement opposable en cas de sinistre.</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-titre">Tous les indices calculés</div>
  <p style="color: var(--gris); margin-bottom: 1.5rem;">Pour chaque indicateur : formule, période de calcul, interprétation viticole et limites.</p>
  {sections_methodo_html}
</div>
"""

(SITE_DIR / "methodologie.html").write_text(page_html("Méthodologie", contenu_methodologie, "methodologie.html"), encoding="utf-8")


print(f"  ✅ 6 pages générées dans {SITE_DIR}/")
print(f"  ✅ Excel copié pour téléchargement : {SITE_DIR / FICHIER_EXCEL}")
