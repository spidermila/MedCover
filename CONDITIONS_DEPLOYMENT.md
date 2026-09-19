# Nasazení podmínkového obsazení

Jde o aditivní změnu. Existující akce zůstávají v režimu `SPOTS`; běžné nové
akce a šablony používají `CONDITIONS`. Klon a rozdělení zachovají režim zdroje.
Žádný krok automaticky nemaže ani nepřevádí stávající akce, účast nebo debriefingy.

## Před nasazením

1. Zálohovat databázi a ověřit obnovu. Zastavit zápisy aplikace i plánovače po
   dobu migrace; stará aplikace nezná přímou účast bez pozice.
2. Staré šablony **není potřeba před migrací mazat**. Zůstanou v seznamu jako
   původní šablony pouze ke čtení, včetně popisu, pozic, kvalifikací a vybavení.
   Po nasazení podle nich ručně vytvořte nové podmínkové šablony; původní pak
   můžete ručně smazat. Nelze je upravovat ani z nich vytvářet akce.
3. Ověřit acykličnost kvalifikačního grafu. V prostředí nové aplikace před
   migrací lze použít Python (používá pouze již existující kvalifikační tabulky):

   ```python
   from app import create_app
   from app.staffing import qualification_graph

   app = create_app()
   with app.app_context():
       qualification_graph()  # ValueError znamená cyklus; před nasazením jej opravte.
   ```

4. Uložit výsledek následujících čtecích SQL dotazů pro porovnání po migraci:

   ```sql
   SELECT COUNT(*) AS events FROM [event];
   SELECT COUNT(*) AS spots FROM event_spot;
   SELECT COUNT(*) AS assignments FROM assignment;
   SELECT COUNT(*) AS debriefings FROM debriefing_record;
   SELECT COUNT(*) AS templates FROM event_template;
   SELECT COUNT(*) AS template_spots FROM event_spot_template;
   SELECT COUNT(*) AS template_qualifications FROM spot_template_qualifications;
   SELECT COUNT(*) AS template_equipment FROM event_template_equipment_plan;
   SELECT id, name, status, start_datetime FROM [event]
   WHERE status NOT IN ('COMPLETED', 'CANCELLED') ORDER BY start_datetime;
   SELECT s.event_id, a.user_id, COUNT(*) AS duplicate_count
   FROM assignment a JOIN event_spot s ON s.id = a.spot_id
   GROUP BY s.event_id, a.user_id HAVING COUNT(*) > 1;
   ```

   Duplicity účasti musí obsluha posoudit před migrací; automaticky se nemažou
   ani neslučují (mohou mít debriefingy). Migrace při duplicitách skončí
   s diagnostikou. Uložený seznam nedokončených akcí je seznam legacy regresí.

## Migrace a ověření

Spustit běžný migrační postup projektu `flask db upgrade`. Revize
`9e8f7a6b5c4d` přidává schéma a backfill, `a1b2c3d4e5f6` eviduje automatické
uzavření kapacitou. Revize `b2c3d4e5f6a7` dovolí prázdné kapacity původních
šablon také v databázích, kde již byla aplikována původní varianta migrace.
Existující podmínkové šablony si ponechají celý plán. Prázdná kapacita označuje
původní šablonu; migrace z pozic nevymýšlí podmínkový plán. Porovnat všechny uložené počty s uloženými hodnotami:
počet akcí, pozic, assignmentů, debriefingů, šablon ani jejich vazeb se nemá změnit.

```sql
SELECT COUNT(*) AS missing_event FROM assignment WHERE event_id IS NULL;
SELECT COUNT(*) AS mismatched_event
FROM assignment a JOIN event_spot s ON s.id = a.spot_id
WHERE a.event_id <> s.event_id;
SELECT event_id, user_id, COUNT(*) AS duplicate_count
FROM assignment GROUP BY event_id, user_id HAVING COUNT(*) > 1;
SELECT staffing_mode, COUNT(*) AS events FROM [event] GROUP BY staffing_mode;
```

První dvě hodnoty musí být nula, duplicity prázdné a všechny předchozí akce
`SPOTS`. Ověřit filtrovaný unikátní index `assignment.spot_id IS NOT NULL`
a unikátnost `(event_id, user_id)`. Debriefingy mají stále původní assignment ID.

Před opětovným zapnutím provozu ověřit jednu legacy akci (detail, editace,
přihlášení/odhlášení, RP, debriefing, kalendář a report) a novou podmínkovou
akci (plán, šablona, přihlášení bez kvalifikace, tvrdá kapacita, deficit a RP).
Plná akce s nesplněnými podmínkami musí zůstat viditelná jako problém.
Odhlášení znovu otevírá pouze přihlášky zavřené dosažením kapacity.

Automatické regresní ověření používá samostatnou testovací databázi:

```sh
TEST_DATABASE_URL='mssql+pyodbc://SA:DevPassword123!@localhost:1433/medcover_test?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes' tox -e py314
```

Testy zahrnují skutečný SQL Server souběh dvou přihlášení na poslední místo,
migrační roundtrip, zachování assignment/debriefing ID a kontrolu dávkového
vyhodnocení bez dalších dotazů pro jednotlivé akce či účastníky.

## Návrat a budoucí odstranění legacy schématu

Downgrade základní revize je bezpečný pouze před vytvořením první podmínkové
akce nebo podmínkové šablony; jinak jej ochrana odmítne. Původní šablony
downgrade neblokují a zachovají si pozice i kvalifikace. Opravná revize při
sestupu ponechá nullable kapacity, shodně s opravenou základní revizí. Ochranu neobcházet a nepouštět
starou aplikaci proti podmínkovým datům. Po zahájení provozu upřednostnit opravu
vpřed, případně koordinovanou obnovu zálohy s vyřešením nových zápisů.

Odstranění pozic, jejich tabulek a `Assignment.spot_id` je samostatná budoucí
kontrakční fáze vyžadující vlastní schválení a ověření, že nezbývá budoucí
spotová akce. Toto nasazení ji neprovádí.
