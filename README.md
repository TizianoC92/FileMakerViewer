# FileMaker Viewer

Applicazione Windows per aprire in sola lettura file FileMaker `.fp3`, `.fp5`,
`.fp7` e `.fmp12`, consultare le tabelle e scorrere i record a pagine.

## Avvio

1. Crea il virtual environment con `python -m venv .venv`.
2. Avvia `Avvia.bat`.
3. Premi **Sfoglia…**, scegli un file supportato e premi **Apri**.

Le intestazioni delle colonne ordinano i record; la ricerca parte dopo almeno
3 caratteri. Con più di 10 tabelle viene mostrato un selettore. Il programma
conserva una cache locale per evitare di rileggere ogni volta il file originale.
La finestra **Struttura** mostra tabelle e campi decodificati, e separa le
possibili relazioni dedotte dai nomi dei campi.

## Formati e limiti

- Sono supportate le quattro estensioni elencate nella guida tecnica
  `fmptools/HACKING`: `.fp3`, `.fp5`, `.fp7` e `.fmp12`.
- I file `.fp3` e `.fp5` usano la famiglia binaria legacy; `.fp7` e `.fmp12`
  usano la famiglia moderna. Le intestazioni vengono controllate prima della
  lettura.
- I file protetti da **Encryption at Rest** non sono leggibili da questo
  estrattore.
- Il lettore recupera tabelle, nomi dei campi e valori tabellari. Per `.fp3` e
  `.fp5` espone anche i tipi di campo presenti nei metadati documentati; la
  guida non descrive abbastanza i tipi per `.fp7` e `.fmp12`.
- `fmptools/HACKING` descrive l’estrazione di dati tabellari, ma non il formato
  del grafo relazionale di FileMaker. La scheda **Collegamenti ipotizzati**
  propone corrispondenze basate sui nomi dei campi: non sono relazioni verificate
  né sostituiscono la visualizzazione del grafico relazionale in FileMaker.
- Layout, script, calcoli, privilegi degli account e contenuti esterni di tipo
  container non vengono ricostruiti. La lettura grezza non applica i privilegi
  FileMaker dell’account.
- La cache JSONL resta nella cartella `cache`; log e file recenti si trovano in
  `%LOCALAPPDATA%\FMP12Viewer`. Queste cartelle contengono dati locali e non
  fanno parte del repository.

## Compilazione accelerata

L'applicazione funziona anche con il lettore Python incluso. Per compilare il
lettore con Cython sono necessari Cython e Microsoft C++ Build Tools; esegui
`CompilaCython.bat` dopo aver creato `.venv`.

## Fonte tecnica e attribuzione

La descrizione dei bytecode, dei settori e dei percorsi dei dati è stata presa
da [Evan Miller, `fmptools/HACKING`](https://github.com/evanmiller/fmptools/blob/main/HACKING).
La famiglia dei formati e il comportamento del lettore di riferimento sono
stati confrontati anche con il codice sorgente di
[`evanmiller/fmptools`](https://github.com/evanmiller/fmptools). Il lettore
Python di questo progetto implementa le strutture documentate e include
un'interpretazione dei campi legacy; non include il codice C di `fmptools`.

`fmptools` è distribuito con licenza MIT. La nota di attribuzione e il testo
della licenza sono riportati in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Il formato FileMaker è proprietario; la documentazione citata è basata su
reverse engineering e alcune varianti possono non essere riconosciute.
