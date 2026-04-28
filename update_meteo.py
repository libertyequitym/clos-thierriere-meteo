"""
Robot météo Clos Thierrière — v3 (stockage incrémental)
========================================================
Construit la base climatologique du domaine sur 50 ans (1975 → aujourd'hui)
en fusionnant deux sources :
  - ERA5 (Copernicus, 9 km) : 1975-12-31 → 2020-12-31
  - AROME 1 km (Météo-France) : 2021-01-01 → hier
Toutes deux accessibles via Open-Meteo, gratuit, sans clé.

Stockage incrémental : à chaque exécution, on lit le fichier parquet existant,
et on ne télécharge QUE les jours manquants. Premier lancement = ~10 min,
lancements suivants = ~30 secondes.

Lit aussi le Google Sheet de saisies terrain (dates phénologiques + observations).
Calcule les indicateurs viticoles experts.
Produit un fichier Excel multi-onglets prêt pour le site web.
"""

import io
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import sys
import functools
print = functools.partial(print, flush=True)

# =============================================================================
# 1. PARAMÈTRES DU DOMAINE
# =============================================================================
LATITUDE = 47.4308
LONGITUDE = 0.9572
NOM_DOMAINE = "Clos Thierrière"
DATE_DEBUT_HISTOIRE = date(1990, 1, 1)
DATE_BASCULE_ERA5_AROME = date(2021, 1, 1)
DATE_FIN = date.today() - timedelta(days=1)
FICHIER_EXCEL = "clos_thierriere_climato.xlsx"
FICHIER_BRUTES = "donnees_brutes.parquet"
GSHEET_ID = "1xrqqxom2uDO6jhys0q23xUf2qMV9u9K9skJ3PjhtDwQ"
NORMALE_DEBUT = 1991
NORMALE_FIN = 2020

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
# 2. TÉLÉCHARGEMENT (par tranches, avec retry)
# =============================================================================
def telecharger_periode(url, debut, fin, source_label, mois_par_tranche=12):
    """Télécharge une période depuis Open-Meteo, par tranches, avec retry."""
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
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "start_date": cursor.isoformat(),
            "end_date": fin_tranche.isoformat(),
            "daily": ",".join(VARIABLES_DAILY),
            "timezone": "Europe/Paris",
            "wind_speed_unit": "kmh",
        }
        succes = False
        for tentative in range(5):
            try:
                r = requests.get(url, params=params, timeout=300)
                if r.status_code == 429:
                    attente = 10 * (tentative + 1)
                    print(f"    429 reçu, attente {attente}s...")
                    time.sleep(attente)
                    continue
                r.raise_for_status()
                succes = True
                break
            except requests.exceptions.Timeout:
                attente = 15 * (tentative + 1)
                print(f"    Timeout, nouvelle tentative dans {attente}s...")
                time.sleep(attente)
                continue
            except requests.exceptions.RequestException as e:
                attente = 10 * (tentative + 1)
                print(f"    Erreur réseau ({e}), nouvelle tentative dans {attente}s...")
                time.sleep(attente)
                continue
        if not succes:
            raise RuntimeError(f"Échec après 5 tentatives sur {source_label} {cursor}-{fin_tranche}")

        df_t = pd.DataFrame(r.json()["daily"])
        morceaux.append(df_t)
        print(f"    {cursor} → {fin_tranche} ok ({len(df_t)} jours)")
        cursor = fin_tranche + timedelta(days=1)
        time.sleep(1.5)
    df_full = pd.concat(morceaux, ignore_index=True)
    df_full["source"] = source_label
    return df_full


# =============================================================================
# 3. STRATÉGIE INCRÉMENTALE — ne télécharger que ce qui manque
# =============================================================================
print("ÉTAPE 1/6 — Stratégie incrémentale")

if Path(FICHIER_BRUTES).exists():
    df_existant = pd.read_parquet(FICHIER_BRUTES)
    df_existant["time"] = pd.to_datetime(df_existant["time"])
    derniere_date = df_existant["time"].max().date()
    print(f"  Fichier existant trouvé : {len(df_existant)} jours, jusqu'au {derniere_date}")
else:
    df_existant = pd.DataFrame()
    derniere_date = None
    print(f"  Aucun fichier existant — premier téléchargement complet")

# Ce qu'il faut télécharger
nouveaux_morceaux = []

if derniere_date is None:
    # Premier lancement : tout télécharger
    df_era5 = telecharger_periode(
        "https://archive-api.open-meteo.com/v1/archive",
        DATE_DEBUT_HISTOIRE,
        DATE_BASCULE_ERA5_AROME - timedelta(days=1),
        "ERA5",
        mois_par_tranche=6,
    )
    df_arome = telecharger_periode(
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        DATE_BASCULE_ERA5_AROME,
        DATE_FIN,
        "AROME-1km",
        mois_par_tranche=12,
    )
    nouveaux_morceaux = [df_era5, df_arome]
else:
    # Lancement suivant : on ne complète qu'à partir de la dernière date
    debut_complement = derniere_date + timedelta(days=1)
    if debut_complement > DATE_FIN:
        print(f"  Données déjà à jour ({derniere_date}). Rien à télécharger.")
    else:
        # Partie ERA5 manquante (rare, sauf si fichier ancien)
        if debut_complement < DATE_BASCULE_ERA5_AROME:
            df_era5 = telecharger_periode(
                "https://archive-api.open-meteo.com/v1/archive",
                debut_complement,
                min(DATE_BASCULE_ERA5_AROME - timedelta(days=1), DATE_FIN),
                "ERA5",
                mois_par_tranche=6,
            )
            nouveaux_morceaux.append(df_era5)
            debut_complement = DATE_BASCULE_ERA5_AROME
        # Partie AROME manquante
        if debut_complement <= DATE_FIN:
            df_arome = telecharger_periode(
                "https://historical-forecast-api.open-meteo.com/v1/forecast",
                debut_complement,
                DATE_FIN,
                "AROME-1km",
                mois_par_tranche=12,
            )
            nouveaux_morceaux.append(df_arome)

# Fusion
if nouveaux_morceaux:
    df_nouveau = pd.concat(nouveaux_morceaux, ignore_index=True)
    df_nouveau["time"] = pd.to_datetime(df_nouveau["time"])
    if not df_existant.empty:
        df_brut = pd.concat([df_existant, df_nouveau], ignore_index=True)
        df_brut = df_brut.drop_duplicates(subset=["time"], keep="first")
    else:
        df_brut = df_nouveau
    df_brut = df_brut.sort_values("time").reset_index(drop=True)
    # Sauvegarde incrémentale
    df_brut.to_parquet(FICHIER_BRUTES, index=False)
    print(f"  Sauvegarde brute mise à jour : {len(df_brut)} jours total")
else:
    df_brut = df_existant

df = df_brut.copy()
print(f"  Total après mise à jour : {len(df)} jours sur {df['time'].dt.year.nunique()} années")
print()

# =============================================================================
# 4. RENOMMAGE & VARIABLES DÉRIVÉES
# =============================================================================
print("ÉTAPE 2/6 — Préparation des données")

df = df.rename(columns={
    "time": "Date",
    "temperature_2m_max": "T_max",
    "temperature_2m_min": "T_min",
    "temperature_2m_mean": "T_moy",
    "precipitation_sum": "RR",
    "rain_sum": "Pluie",
    "snowfall_sum": "Neige_cm",
    "precipitation_hours": "RR_heures",
    "sunshine_duration": "Insolation_s",
    "shortwave_radiation_sum": "Rayonnement_MJ_m2",
    "et0_fao_evapotranspiration": "ETP",
    "wind_speed_10m_max": "Vent_max_kmh",
    "wind_gusts_10m_max": "Rafale_kmh",
    "wind_direction_10m_dominant": "Vent_dir",
    "relative_humidity_2m_mean": "HR_moy",
    "relative_humidity_2m_max": "HR_max",
    "relative_humidity_2m_min": "HR_min",
    "dew_point_2m_mean": "Pt_rosee",
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

df["Jour_gel"] = (df["T_min"] <= 0).astype(int)
df["Jour_gel_severe"] = (df["T_min"] <= -2).astype(int)
df["Jour_gel_destructeur"] = (df["T_min"] <= -4).astype(int)
df["Jour_chaud_25"] = (df["T_max"] >= 25).astype(int)
df["Jour_chaud_30"] = (df["T_max"] >= 30).astype(int)
df["Jour_chaud_35"] = (df["T_max"] >= 35).astype(int)
df["Jour_tropical"] = (df["T_min"] >= 20).astype(int)
df["Jour_pluvieux"] = (df["RR"] >= 1).astype(int)
df["Jour_pluvieux_fort"] = (df["RR"] >= 20).astype(int)
df["Jour_sec"] = (df["RR"] < 0.5).astype(int)


# =============================================================================
# 5. INDICES VITICOLES JOURNALIERS
# =============================================================================
print("ÉTAPE 3/6 — Indices viticoles journaliers")


def coef_huglin_lat(lat):
    if lat <= 40: return 1.00
    if lat <= 42: return 1.02
    if lat <= 44: return 1.03
    if lat <= 46: return 1.04
    if lat <= 48: return 1.05
    if lat <= 50: return 1.06
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
# 6. RISQUES SANITAIRES
# =============================================================================
print("ÉTAPE 4/6 — Risques sanitaires")

df["RR_48h"] = df["RR"].rolling(2, min_periods=1).sum()
df["Mildiou_score"] = (
    ((df["T_moy"] >= 10) & (df["T_moy"] <= 25)).astype(int)
    + (df["RR_48h"] >= 10).astype(int)
    + (df["HR_moy"] >= 75).astype(int)
)
df["Mildiou_risque"] = pd.cut(
    df["Mildiou_score"], bins=[-1, 0, 1, 2, 3],
    labels=["Nul", "Faible", "Modéré", "Élevé"]
).astype(str)

df["Oidium_jour_favorable"] = (
    (df["T_max"] >= 21) & (df["T_max"] <= 30) & (df["HR_moy"] >= 60)
).astype(int)
df["Oidium_score"] = df["Oidium_jour_favorable"].rolling(7, min_periods=1).sum()
df["Oidium_risque"] = pd.cut(
    df["Oidium_score"], bins=[-1, 2, 4, 6, 7],
    labels=["Faible", "Modéré", "Élevé", "Très élevé"]
).astype(str)

df["RR_7j"] = df["RR"].rolling(7, min_periods=1).sum()
df["Botrytis_score"] = (
    ((df["HR_moy"] >= 80).astype(int) * 2)
    + ((df["RR_7j"] >= 30).astype(int) * 2)
    + ((df["T_moy"] >= 15) & (df["T_moy"] <= 22)).astype(int)
)
df["Botrytis_risque"] = pd.cut(
    df["Botrytis_score"], bins=[-1, 1, 2, 3, 5],
    labels=["Faible", "Modéré", "Élevé", "Très élevé"]
).astype(str)

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
df["Gel_printanier"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min"] <= 0)).astype(int)
df["Gel_printanier_severe"] = ((df["Mois"].isin([3, 4, 5])) & (df["T_min"] <= -2)).astype(int)


# =============================================================================
# 7. SYNTHÈSES
# =============================================================================
print("ÉTAPE 5/6 — Synthèses mensuelles, annuelles, décennies")

synthese_mens = df.groupby(["Annee", "Mois"]).agg(**{
    "T_min_moy": ("T_min", "mean"),
    "T_max_moy": ("T_max", "mean"),
    "T_moy": ("T_moy", "mean"),
    "T_max_abs": ("T_max", "max"),
    "T_min_abs": ("T_min", "min"),
    "Amplitude_moy": ("Amplitude_thermique", "mean"),
    "RR_total": ("RR", "sum"),
    "ETP_total": ("ETP", "sum"),
    "Bilan_hydrique": ("Bilan_hydrique_J", "sum"),
    "Insolation_h": ("Insolation_h", "sum"),
    "Rayonnement_MJ_m2": ("Rayonnement_MJ_m2", "sum"),
    "Vent_max_moy_kmh": ("Vent_max_kmh", "mean"),
    "HR_moy_pct": ("HR_moy", "mean"),
    "Jours_gel": ("Jour_gel", "sum"),
    "Jours_gel_severe": ("Jour_gel_severe", "sum"),
    "Jours_chauds_30": ("Jour_chaud_30", "sum"),
    "Jours_chauds_35": ("Jour_chaud_35", "sum"),
    "Nuits_tropicales": ("Jour_tropical", "sum"),
    "Jours_pluvieux": ("Jour_pluvieux", "sum"),
    "Jours_secs": ("Jour_sec", "sum"),
    "Mildiou_jours_eleves": ("Mildiou_score", lambda s: int((s >= 3).sum())),
    "Oidium_jours_eleves": ("Oidium_score", lambda s: int((s >= 5).sum())),
    "Fenetres_traitement": ("Fenetre_traitement_OK", "sum"),
}).round(1).reset_index()

masque_normale = (df["Annee"] >= NORMALE_DEBUT) & (df["Annee"] <= NORMALE_FIN)
normales_mens = df[masque_normale].groupby("Mois").agg(**{
    "T_min_normale": ("T_min", "mean"),
    "T_max_normale": ("T_max", "mean"),
    "T_moy_normale": ("T_moy", "mean"),
    "RR_normale": ("RR", "sum"),
    "ETP_normale": ("ETP", "sum"),
    "Insolation_normale_h": ("Insolation_h", "sum"),
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
        "Millesime": an,
        "T_moy_annuelle": round(sous["T_moy"].mean(), 2),
        "T_max_max": round(sous["T_max"].max(), 1),
        "T_min_min": round(sous["T_min"].min(), 1),
        "RR_totale_mm": round(pluie_an, 1),
        "RR_campagne_mm": round(pluie_camp, 1),
        "ETP_campagne_mm": round(etp_camp, 1),
        "Bilan_hydrique_campagne": riou,
        "Indice_Huglin": round(huglin, 0),
        "Classe_Huglin": classe_huglin,
        "Indice_Winkler": round(winkler, 0),
        "Classe_Winkler": classe_winkler,
        "IF_septembre": round(if_nuits, 2) if if_nuits is not None else None,
        "Classe_IF_nuits": classe_if,
        "Indice_Selianinov": selianinov,
        "Amplitude_thermique_aout": round(amplitude_aout, 1) if amplitude_aout is not None else None,
        "Jours_gel_total": int(sous["Jour_gel"].sum()),
        "Jours_gel_printanier": int(sous["Gel_printanier"].sum()),
        "Jours_gel_printanier_severe": int(sous["Gel_printanier_severe"].sum()),
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
    "Jours_gel_an_moy": ("Jour_gel", lambda s: s.sum() / (s.index.size / 365.25)),
    "Jours_chauds_30_an_moy": ("Jour_chaud_30", lambda s: s.sum() / (s.index.size / 365.25)),
    "Jours_chauds_35_an_moy": ("Jour_chaud_35", lambda s: s.sum() / (s.index.size / 365.25)),
    "Nuits_tropicales_an_moy": ("Jour_tropical", lambda s: s.sum() / (s.index.size / 365.25)),
}).round(2).reset_index()


# =============================================================================
# 8. GOOGLE SHEET
# =============================================================================
print("ÉTAPE 6/6 — Lecture du Google Sheet")


def lire_gsheet_onglet(sheet_id, nom_onglet):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={nom_onglet}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        print(f"  ⚠️  Onglet '{nom_onglet}' non lu : {e}")
        return pd.DataFrame()


df_phenologie = lire_gsheet_onglet(GSHEET_ID, "phenologie")
df_observations = lire_gsheet_onglet(GSHEET_ID, "observations")
print(f"  Phénologie : {len(df_phenologie)} lignes")
print(f"  Observations : {len(df_observations)} lignes")


# =============================================================================
# 9. README & ÉCRITURE EXCEL
# =============================================================================
readme = [
    ["Domaine", NOM_DOMAINE],
    ["Localisation", "Vernou-sur-Brenne (37210), AOC Vouvray"],
    ["Coordonnées GPS", f"{LATITUDE}°N, {LONGITUDE}°E"],
    ["", ""],
    ["SOURCES DE DONNÉES", ""],
    ["1975 → 2020", "ERA5 (Copernicus / ECMWF) — 9 km, réanalyse climatique mondiale"],
    ["2021 → aujourd'hui", "AROME 1 km — modèle haute résolution Météo-France"],
    ["Accès", "Open-Meteo (gratuit, sans clé API)"],
    ["", ""],
    ["MISE À JOUR", "Quotidienne automatique via GitHub Actions"],
    ["Stockage incrémental", "Fichier parquet local — ne télécharge que les nouveaux jours"],
    ["Dernière exécution", date.today().isoformat()],
    ["Période couverte", f"{df['Date'].min().date()} → {df['Date'].max().date()}"],
    ["Nombre total de jours", str(len(df))],
    ["Nombre d'années", str(df["Annee"].nunique())],
    ["", ""],
    ["NORMALES CLIMATIQUES", f"{NORMALE_DEBUT}-{NORMALE_FIN} (standard OMM)"],
    ["", ""],
    ["INDICES VITICOLES", ""],
    ["Huglin", "Σ [(Tmoy-10)+(Tmax-10)]/2 × K_lat, du 01/04 au 30/09"],
    ["Winkler (GDD)", "Σ max(0, Tmoy-10), du 01/04 au 31/10"],
    ["Fraîcheur des Nuits (IF)", "Moyenne des Tmin de septembre (Tonietto 1999)"],
    ["GFV Parker", "Σ Tmoy base 0°C depuis 01/01"],
    ["GSR", "Σ Tmoy base 0°C de la véraison à la maturité"],
    ["BBCH base 5°C", "Cumul T° pour modélisation débourrement"],
    ["Riou (sécheresse)", "Bilan hydrique avril-septembre"],
    ["Selianinov", "(ΣP campagne / Winkler) × 10"],
    ["", ""],
    ["RISQUES SANITAIRES", ""],
    ["Mildiou", "Modèle Goidanich simplifié"],
    ["Oïdium", "Modèle Gubler-Thomas"],
    ["Botrytis", "Score HR + RR 7j + T° favorable"],
    ["Échaudage", "T_max ≥ 35°C avec RFU < 60mm"],
    ["", ""],
    ["BILAN HYDRIQUE", ""],
    ["RFU max", f"{RFU_MAX} mm (sols argilo-calcaires Vouvray)"],
    ["", ""],
    ["GOOGLE SHEET — saisies terrain", ""],
    ["Lien", f"https://docs.google.com/spreadsheets/d/{GSHEET_ID}"],
    ["Phénologie", f"{len(df_phenologie)} millésimes saisis"],
    ["Observations", f"{len(df_observations)} observations terrain"],
]
df_readme = pd.DataFrame(readme, columns=["Élément", "Valeur"])

print()
print(f"Écriture du fichier {FICHIER_EXCEL}...")

COLS_BRUTES = [
    "Date", "Source", "Annee", "Mois", "Jour", "Jour_julien", "Semaine_ISO",
    "T_min", "T_max", "T_moy", "Amplitude_thermique",
    "RR", "Pluie", "Neige_cm", "RR_heures",
    "ETP", "Bilan_hydrique_J",
    "HR_min", "HR_moy", "HR_max", "Pt_rosee",
    "Vent_max_kmh", "Rafale_kmh", "Vent_dir",
    "Insolation_h", "Rayonnement_MJ_m2",
    "Jour_gel", "Jour_gel_severe", "Jour_chaud_25", "Jour_chaud_30", "Jour_chaud_35",
    "Jour_tropical", "Jour_pluvieux", "Jour_pluvieux_fort", "Jour_sec",
]
COLS_INDICES = [
    "Date", "Annee", "Mois",
    "T_min", "T_max", "T_moy", "Amplitude_thermique",
    "GDD_base10", "GDD_base5", "GDD_base0",
    "Contrib_Winkler", "Contrib_Huglin", "Contrib_GFV", "Contrib_BBCH",
    "Winkler_cumul", "Huglin_cumul", "GFV_cumul", "BBCH5_cumul",
    "RR", "RR_cumul_campagne", "ETP", "ETP_cumul_campagne",
    "Bilan_hydrique_J", "Bilan_hydrique_cumul", "RFU_mm", "Stress_hydrique",
    "Gel_printanier", "Gel_printanier_severe", "Echaudage_jour",
    "Jour_ideal_maturation", "Jour_ideal_vendange",
]
COLS_RISQUES = [
    "Date", "Annee", "Mois",
    "T_moy", "T_max", "RR", "RR_48h", "RR_7j", "HR_moy",
    "Mildiou_score", "Mildiou_risque",
    "Oidium_score", "Oidium_risque",
    "Botrytis_score", "Botrytis_risque",
    "Echaudage_jour", "Fenetre_traitement_OK",
]

with pd.ExcelWriter(FICHIER_EXCEL, engine="openpyxl") as writer:
    df_readme.to_excel(writer, sheet_name="README", index=False)
    df_millesimes.to_excel(writer, sheet_name="Millesimes", index=False)
    synthese_dec.to_excel(writer, sheet_name="Decennies_50ans", index=False)
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
print(f"   Fichier brut : {FICHIER_BRUTES} (incrémental)")
print(f"   Fichier Excel : {FICHIER_EXCEL}")
