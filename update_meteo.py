"""
Robot météo du Clos Thierrière
==============================
Télécharge les données météo quotidiennes pour Vernou-sur-Brenne
depuis le 01/01/2021 jusqu'à aujourd'hui, calcule les indices viticoles
(Huglin, Winkler, fraîcheur des nuits, gel, etc.), et produit un fichier
Excel multi-onglets prêt à l'emploi.

Source : Open-Meteo Historical Forecast API (modèles AROME 1km de Météo-France).
Licence : libre, pas de clé API requise.
"""

import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

# -----------------------------------------------------------------------------
# 1. PARAMÈTRES DU DOMAINE
# -----------------------------------------------------------------------------
LATITUDE = 47.4308          # Vernou-sur-Brenne, 210 rue Neuve (centroïde du Clos)
LONGITUDE = 0.9572
NOM_DOMAINE = "Clos Thierrière"
DATE_DEBUT = date(2021, 1, 1)
DATE_FIN = date.today() - timedelta(days=1)   # données disponibles jusqu'à J-1
FICHIER_EXCEL = "clos_thierriere_climato.xlsx"

# -----------------------------------------------------------------------------
# 2. TÉLÉCHARGEMENT DES DONNÉES
# -----------------------------------------------------------------------------
print(f"Téléchargement météo pour {NOM_DOMAINE} ({LATITUDE}, {LONGITUDE})")
print(f"Période : {DATE_DEBUT} → {DATE_FIN}")

url = "https://archive-api.open-meteo.com/v1/archive"
params = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "start_date": DATE_DEBUT.isoformat(),
    "end_date": DATE_FIN.isoformat(),
    "daily": ",".join([
        "temperature_2m_max",
        "temperature_2m_min",
        "temperature_2m_mean",
        "apparent_temperature_max",
        "apparent_temperature_min",
        "precipitation_sum",
        "rain_sum",
        "snowfall_sum",
        "precipitation_hours",
        "sunshine_duration",
        "daylight_duration",
        "shortwave_radiation_sum",
        "et0_fao_evapotranspiration",
        "wind_speed_10m_max",
        "wind_gusts_10m_max",
        "wind_direction_10m_dominant",
        "relative_humidity_2m_mean",
        "relative_humidity_2m_max",
        "relative_humidity_2m_min",
        "dew_point_2m_mean",
        "surface_pressure_mean",
    ]),
    "timezone": "Europe/Paris",
    "wind_speed_unit": "kmh",
}

reponse = requests.get(url, params=params, timeout=60)
reponse.raise_for_status()
donnees = reponse.json()["daily"]

# -----------------------------------------------------------------------------
# 3. CONSTRUCTION DU TABLEAU DES DONNÉES BRUTES
# -----------------------------------------------------------------------------
df = pd.DataFrame(donnees)
df["time"] = pd.to_datetime(df["time"])

# Renommage en français
df = df.rename(columns={
    "time": "Date",
    "temperature_2m_max": "T_max_°C",
    "temperature_2m_min": "T_min_°C",
    "temperature_2m_mean": "T_moy_°C",
    "apparent_temperature_max": "T_ressentie_max_°C",
    "apparent_temperature_min": "T_ressentie_min_°C",
    "precipitation_sum": "Précipitations_mm",
    "rain_sum": "Pluie_mm",
    "snowfall_sum": "Neige_cm",
    "precipitation_hours": "Heures_précipitations",
    "sunshine_duration": "Insolation_secondes",
    "daylight_duration": "Durée_jour_secondes",
    "shortwave_radiation_sum": "Rayonnement_global_MJ_m2",
    "et0_fao_evapotranspiration": "ETP_mm",
    "wind_speed_10m_max": "Vent_max_kmh",
    "wind_gusts_10m_max": "Rafale_max_kmh",
    "wind_direction_10m_dominant": "Vent_direction_°",
    "relative_humidity_2m_mean": "HR_moy_%",
    "relative_humidity_2m_max": "HR_max_%",
    "relative_humidity_2m_min": "HR_min_%",
    "dew_point_2m_mean": "Point_rosée_°C",
    "surface_pressure_mean": "Pression_hPa",
})

# Conversions et colonnes additionnelles
df["Insolation_h"] = (df["Insolation_secondes"] / 3600).round(2)
df.drop(columns=["Insolation_secondes", "Durée_jour_secondes"], inplace=True)
df["Amplitude_thermique_°C"] = (df["T_max_°C"] - df["T_min_°C"]).round(2)
df["Bilan_hydrique_mm"] = (df["Précipitations_mm"] - df["ETP_mm"]).round(2)
df["Année"] = df["Date"].dt.year
df["Mois"] = df["Date"].dt.month
df["Jour_julien"] = df["Date"].dt.dayofyear
df["Semaine_ISO"] = df["Date"].dt.isocalendar().week

# Indicateurs binaires utiles
df["Jour_gel"] = (df["T_min_°C"] <= 0).astype(int)
df["Jour_gel_sévère"] = (df["T_min_°C"] <= -2).astype(int)
df["Jour_chaud_30"] = (df["T_max_°C"] >= 30).astype(int)
df["Jour_chaud_35"] = (df["T_max_°C"] >= 35).astype(int)
df["Jour_pluvieux"] = (df["Précipitations_mm"] >= 1).astype(int)
df["Jour_très_pluvieux"] = (df["Précipitations_mm"] >= 20).astype(int)

# -----------------------------------------------------------------------------
# 4. CALCUL DES INDICES VITICOLES
# -----------------------------------------------------------------------------

# Coefficient K de Huglin pour la latitude (47.43°N → ~1.05)
def coef_huglin(latitude):
    if latitude <= 40: return 1.00
    if latitude <= 42: return 1.02
    if latitude <= 44: return 1.03
    if latitude <= 46: return 1.04
    if latitude <= 48: return 1.05
    if latitude <= 50: return 1.06
    return 1.06

K_HUGLIN = coef_huglin(LATITUDE)

# Contributions journalières aux indices (calculées sur tout l'année,
# on filtrera ensuite sur la fenêtre de chaque indice).
df["GDD_base10"] = ((df["T_moy_°C"] - 10).clip(lower=0)).round(2)
df["Contrib_Winkler"] = df["GDD_base10"]
df["Contrib_Huglin"] = (((df["T_moy_°C"] - 10).clip(lower=0)
                        + (df["T_max_°C"] - 10).clip(lower=0)) / 2 * K_HUGLIN).round(2)
df["Contrib_GFV"] = df["T_moy_°C"].clip(lower=0)  # GFV : base 0°C dès le 1er janv

# Cumuls glissants par campagne (1er avril → 31 octobre pour Winkler/Huglin)
df["Winkler_cumul"] = 0.0
df["Huglin_cumul"] = 0.0
df["GFV_cumul"] = 0.0
df["Pluie_cumul_campagne_mm"] = 0.0
df["ETP_cumul_campagne_mm"] = 0.0

for annee in df["Année"].unique():
    masque_an = df["Année"] == annee
    masque_winkler = masque_an & (df["Date"].dt.month >= 4) & (df["Date"].dt.month <= 10)
    masque_huglin = masque_an & (df["Date"].dt.month >= 4) & (df["Date"].dt.month <= 9)
    masque_gfv = masque_an   # depuis le 1er janvier
    masque_camp = masque_an & (df["Date"].dt.month >= 4) & (df["Date"].dt.month <= 9)

    df.loc[masque_winkler, "Winkler_cumul"] = df.loc[masque_winkler, "Contrib_Winkler"].cumsum().round(1)
    df.loc[masque_huglin, "Huglin_cumul"] = df.loc[masque_huglin, "Contrib_Huglin"].cumsum().round(1)
    df.loc[masque_gfv, "GFV_cumul"] = df.loc[masque_gfv, "Contrib_GFV"].cumsum().round(1)
    df.loc[masque_camp, "Pluie_cumul_campagne_mm"] = df.loc[masque_camp, "Précipitations_mm"].cumsum().round(1)
    df.loc[masque_camp, "ETP_cumul_campagne_mm"] = df.loc[masque_camp, "ETP_mm"].cumsum().round(1)

df["Bilan_hydrique_cumul_mm"] = (df["Pluie_cumul_campagne_mm"] - df["ETP_cumul_campagne_mm"]).round(1)

# -----------------------------------------------------------------------------
# 5. SYNTHÈSE MENSUELLE
# -----------------------------------------------------------------------------
synthese_mens = df.groupby(["Année", "Mois"]).agg(**{
       "T_min_moy_°C": ("T_min_°C", "mean"),
       "T_max_moy_°C": ("T_max_°C", "mean"),
       "T_moy_°C": ("T_moy_°C", "mean"),
       "Précipitations_mm": ("Précipitations_mm", "sum"),
       "ETP_mm": ("ETP_mm", "sum"),
       "Bilan_hydrique_mm": ("Bilan_hydrique_mm", "sum"),
       "Insolation_h": ("Insolation_h", "sum"),
       "Rayonnement_MJ_m2": ("Rayonnement_global_MJ_m2", "sum"),
       "Vent_max_moy_kmh": ("Vent_max_kmh", "mean"),
       "HR_moy_pct": ("HR_moy_%", "mean"),
       "Jours_gel": ("Jour_gel", "sum"),
       "Jours_gel_sévère": ("Jour_gel_sévère", "sum"),
       "Jours_chauds_30": ("Jour_chaud_30", "sum"),
       "Jours_chauds_35": ("Jour_chaud_35", "sum"),
       "Jours_pluvieux": ("Jour_pluvieux", "sum"),
   }).round(1).reset_index()

# -----------------------------------------------------------------------------
# 6. SYNTHÈSE ANNUELLE / FICHE MILLÉSIME
# -----------------------------------------------------------------------------
fiches_millesime = []
for annee in sorted(df["Année"].unique()):
    sous = df[df["Année"] == annee]
    fenetre_winkler = sous[(sous["Date"].dt.month >= 4) & (sous["Date"].dt.month <= 10)]
    fenetre_huglin = sous[(sous["Date"].dt.month >= 4) & (sous["Date"].dt.month <= 9)]
    fenetre_campagne = sous[(sous["Date"].dt.month >= 4) & (sous["Date"].dt.month <= 9)]
    gel_printanier = sous[(sous["Date"].dt.month.isin([3, 4, 5])) & (sous["Jour_gel"] == 1)]
    septembre = sous[sous["Date"].dt.month == 9]

    huglin = fenetre_huglin["Contrib_Huglin"].sum()
    winkler = fenetre_winkler["Contrib_Winkler"].sum()
    if_nuits = septembre["T_min_°C"].mean() if len(septembre) else None
    pluie_camp = fenetre_campagne["Précipitations_mm"].sum()
    etp_camp = fenetre_campagne["ETP_mm"].sum()

    # Classes selon Tonietto
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

    fiches_millesime.append({
        "Millésime": annee,
        "T_moy_annuelle_°C": round(sous["T_moy_°C"].mean(), 2),
        "Précipitations_totales_mm": round(sous["Précipitations_mm"].sum(), 1),
        "Précipitations_campagne_avr_sept_mm": round(pluie_camp, 1),
        "ETP_campagne_avr_sept_mm": round(etp_camp, 1),
        "Bilan_hydrique_campagne_mm": round(pluie_camp - etp_camp, 1),
        "Indice_Huglin": round(huglin, 0),
        "Classe_Huglin": classe_huglin,
        "Indice_Winkler_GDD": round(winkler, 0),
        "Classe_Winkler": classe_winkler,
        "IF_Tmin_septembre_°C": round(if_nuits, 2) if if_nuits is not None else None,
        "Classe_IF_nuits": classe_if,
        "Jours_gel_année": int(sous["Jour_gel"].sum()),
        "Jours_gel_printanier_marsavrimai": int(len(gel_printanier)),
        "Jours_chauds_30°C": int(sous["Jour_chaud_30"].sum()),
        "Jours_chauds_35°C": int(sous["Jour_chaud_35"].sum()),
        "Jours_pluvieux": int(sous["Jour_pluvieux"].sum()),
        "Insolation_totale_h": round(sous["Insolation_h"].sum(), 0),
    })

df_millesimes = pd.DataFrame(fiches_millesime)

# -----------------------------------------------------------------------------
# 7. ONGLET README
# -----------------------------------------------------------------------------
readme_lignes = [
    ["Domaine", NOM_DOMAINE],
    ["Localisation", "Vernou-sur-Brenne (37210), AOC Vouvray"],
    ["Coordonnées GPS", f"{LATITUDE}, {LONGITUDE}"],
    ["Source des données", "Open-Meteo Historical Forecast API (modèle AROME 1km, Météo-France)"],
    ["Période couverte", f"{DATE_DEBUT} → {DATE_FIN}"],
    ["Mise à jour", "Quotidienne automatique via GitHub Actions"],
    ["Dernière exécution", date.today().isoformat()],
    ["", ""],
    ["Onglet Données_brutes_J", "Mesures journalières brutes (T, pluie, vent, ETP, etc.)"],
    ["Onglet Indices_J", "Mêmes données + cumuls et indices viticoles journaliers"],
    ["Onglet Synthèse_mensuelle", "Agrégats mensuels (moyennes, cumuls, compteurs)"],
    ["Onglet Millésimes", "Fiche annuelle par millésime, avec indices Huglin/Winkler/IF"],
    ["", ""],
    ["Indice Huglin", "Σ [(Tmoy-10)+(Tmax-10)]/2 × K, du 01/04 au 30/09. K=1.05 à 47.43°N"],
    ["Indice Winkler (GDD)", "Σ max(0, Tmoy-10), du 01/04 au 31/10"],
    ["Indice de Fraîcheur des Nuits", "Moyenne des Tmin du mois de septembre (Tonietto)"],
    ["GFV (Parker)", "Σ Tmoy base 0°C depuis le 01/01 — prédit floraison/véraison"],
    ["Bilan hydrique campagne", "Pluies − ETP cumulés du 01/04 au 30/09"],
]
df_readme = pd.DataFrame(readme_lignes, columns=["Élément", "Valeur"])

# -----------------------------------------------------------------------------
# 8. ÉCRITURE DU FICHIER EXCEL
# -----------------------------------------------------------------------------
print(f"Écriture du fichier {FICHIER_EXCEL}...")

# Tri colonnes données brutes
colonnes_brutes = [
    "Date", "Année", "Mois", "Jour_julien", "Semaine_ISO",
    "T_min_°C", "T_max_°C", "T_moy_°C", "Amplitude_thermique_°C",
    "T_ressentie_min_°C", "T_ressentie_max_°C",
    "Précipitations_mm", "Pluie_mm", "Neige_cm", "Heures_précipitations",
    "ETP_mm", "Bilan_hydrique_mm",
    "HR_min_%", "HR_moy_%", "HR_max_%", "Point_rosée_°C",
    "Vent_max_kmh", "Rafale_max_kmh", "Vent_direction_°",
    "Insolation_h", "Rayonnement_global_MJ_m2", "Pression_hPa",
    "Jour_gel", "Jour_gel_sévère", "Jour_chaud_30", "Jour_chaud_35",
    "Jour_pluvieux", "Jour_très_pluvieux",
]
df_brutes = df[colonnes_brutes]

colonnes_indices = [
    "Date", "Année", "Mois",
    "T_min_°C", "T_max_°C", "T_moy_°C",
    "GDD_base10", "Contrib_Winkler", "Contrib_Huglin", "Contrib_GFV",
    "Winkler_cumul", "Huglin_cumul", "GFV_cumul",
    "Précipitations_mm", "Pluie_cumul_campagne_mm",
    "ETP_mm", "ETP_cumul_campagne_mm",
    "Bilan_hydrique_mm", "Bilan_hydrique_cumul_mm",
]
df_indices = df[colonnes_indices]

with pd.ExcelWriter(FICHIER_EXCEL, engine="openpyxl") as writer:
    df_readme.to_excel(writer, sheet_name="README", index=False)
    df_millesimes.to_excel(writer, sheet_name="Millésimes", index=False)
    synthese_mens.to_excel(writer, sheet_name="Synthèse_mensuelle", index=False)
    df_indices.to_excel(writer, sheet_name="Indices_J", index=False)
    df_brutes.to_excel(writer, sheet_name="Données_brutes_J", index=False)

print(f"✅ Terminé. {len(df)} jours téléchargés depuis le {DATE_DEBUT}.")
print(f"   Fichier produit : {FICHIER_EXCEL}")
