# Verbatim from Wes's live database (project 12, OpenJournal): eight open
# questions, filed over 27 hours by six separate runs, every one of them
# asking him to pick the same name. This is the corpus the matcher was
# fitted against - their pairwise word overlap is only 0.34-0.67, so no
# wording threshold separates them from unrelated questions. What is
# identical is the menu of answers.
NAME_QUESTIONS = [
    (
        "Off-line turns out to be taken, and badly — American Eagle owns offline.com for "
        "their OFFLINE by Aerie brand and enforces it, there's already an 'Offline Social' "
        "app on both app stores, and 'Offline Diary' and 'Offline Journal' are both shipping "
        "journaling apps. Peek and Noted are taken too (peek.com is a funded travel company, "
        "notedapp.io is a notes app with a trademark claim). The real killer is search: "
        "'offline' is one of the commonest words in computing, so the project would never "
        "rank for its own name on Google, GitHub or npm — which matters when the only way "
        "people find it is word of mouth. Want to go with a coined name instead? Kithlog is "
        "my pick: 'kith' as in kith and kin, the old word for exactly your circle of friends, "
        "and the .com, .org, npm and GitHub names are all free with no prior art anywhere."
        , '["Kithlog", "Porchlog", "keep OpenJournal for now", "I\'ll think of one"]'),
    (
        "OpenJournal needs a final name before it gets a public repo, a domain and a Docker "
        "image — which is the next phase of work. I researched your suggestion 'Off-line' and "
        "it's taken comprehensively: offline.com belongs to American Eagle's OFFLINE by Aerie "
        "(who sued Amazon over it in 2024), there's an Offline Social app on both stores, "
        "plus Offline Diary and Offline Journal on Play — your exact category. The bigger "
        "problem is search: 'offline' is one of the most common words in computing, so you'd "
        "never rank for your own project's name. Peek and Noted lost for the same kind of "
        "reason (peek.com is a funded travel company; phw/peek is a 10.5k-star GIF recorder; "
        "notedapp.io has a trademark claim). My pick is Kithlog — 'kith', as in kith and kin, "
        "the old word for exactly your circle of friends, which is the product thesis in one "
        "syllable. The .com/.org/.app, npm and GitHub are all free with no prior art. "
        "Porchlog is the runner-up, also free."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "I\'ll think of one"]'),
    (
        "OpenJournal needs a final name before it gets a public repo, a domain and a Docker "
        "image — which is the next phase. You asked about 'Off-line': it's taken (American "
        "Eagle's OFFLINE by Aerie brand, which they enforce; an 'Offline Social' app on both "
        "stores; 'Offline Diary' and 'Offline Journal' on Play), and worse, 'offline' is one "
        "of the most common words in computing, so the project would never rank for its own "
        "name in Google, GitHub, npm or an app store. Peek and Noted lost the same way. My "
        "pick is Kithlog — 'kith', as in kith and kin, is the old word for exactly your "
        "circle of friends, which is the product thesis in one syllable, and the "
        ".com/.org/.app, npm and GitHub are all free with no prior art. Porchlog is the "
        "runner-up (a porch light left on for the few people who actually come by)."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "I\'ll think of one"]'),
    (
        "The name is the one thing still open, and it starts to bite in the next step — "
        "Docker packaging is where a repo, a domain and an image name all get chosen at once. "
        "You asked about Off-line last time; I found it's taken and worse than what it'd "
        "replace (American Eagle's OFFLINE by Aerie owns offline.com and sued over the brand "
        "in 2024, there's an Offline Social app on both stores, plus Offline Diary and "
        "Offline Journal on Play — your exact category). The real disqualifier is search: "
        "'offline' is one of the most common words in computing, so you'd never rank for your "
        "own project's name anywhere. Peek and Noted lost too (peek.com is a funded travel "
        "company, phw/peek is a 10.5k-star GIF recorder, notedapp.io has a trademark claim). "
        "My pick is Kithlog — kith, as in kith and kin, the old word for exactly your circle "
        "of friends. .com/.org/.app, npm and the GitHub org are all free with no prior art. "
        "Porchlog is the runner-up."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "I\'ll think about it"]'),
    (
        "The name is now the only thing standing between OpenJournal and a public repo + "
        "domain, and it gets expensive the moment either exists — the hostname becomes your "
        "handle (@wes@journal.example.com) and every friend's node pins it, so changing it "
        "later means re-introducing yourself to everyone. You asked about 'Off-line' last "
        "time: it's taken comprehensively (American Eagle's OFFLINE by Aerie brand, an "
        "Offline Social app on both stores, Offline Diary and Offline Journal on Play), and "
        "worse, 'offline' is one of the most common words in computing so you'd never rank "
        "for your own project's name in a search — which is the whole word-of-mouth growth "
        "mechanism. Peek and Noted lost the same way. My pick is Kithlog — 'kith', as in kith "
        "and kin, the old word for exactly your circle of friends, which is the product "
        "thesis in one syllable, and the .com/.org/.app, npm and GitHub org were all free. "
        "Porchlog is the runner-up."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "let me think"]'),
    (
        "The name is the one thing still blocking OpenJournal from going public, and it's "
        "been open since Friday. 'Off-line' is taken comprehensively (American Eagle's "
        "OFFLINE by Aerie owns offline.com and sued Amazon over the brand in 2024; there's an "
        "Offline Social app on both stores, plus Offline Diary and Offline Journal on Play) — "
        "and the bigger problem is search: 'offline' is one of the most common words in "
        "computing, so the project would never rank for its own name in Google, GitHub, npm "
        "or an app store, and word-of-mouth plus 'search the name I heard' is the whole "
        "growth mechanism. Peek and Noted lost the same way. My pick is Kithlog — 'kith', as "
        "in kith and kin, the old word for exactly your circle of friends, which is the "
        "product thesis in one syllable, and the .com/.org/.app, npm and the GitHub org were "
        "all free. Porchlog is the runner-up. Everything buildable that does not depend on "
        "this is now done, so this is genuinely the next step."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "let me think"]'),
    (
        "The name — this is the last thing blocking OpenJournal from going public, and it's "
        "yours to call. You asked about 'Off-line': it's taken, comprehensively. offline.com "
        "is American Eagle's OFFLINE by Aerie brand (they sued Amazon over it in 2024), "
        "there's an Offline Social app shipping on both app stores, and Offline Diary and "
        "Offline Journal are both on Google Play — your exact category. The bigger problem "
        "isn't legal, it's search: 'offline' is one of the most common words in computing, so "
        "the project would never rank for its own name in Google, GitHub, npm or an app "
        "store, and word-of-mouth plus 'search the name I heard' is the whole growth "
        "mechanism. Peek and Noted lost the same way (peek.com is a funded travel company, "
        "phw/peek is a 10.5k-star GIF recorder, notedapp.io has a trademark claim). My pick "
        "is Kithlog — 'kith', as in kith and kin, the old word for exactly your circle of "
        "friends, which is the product thesis in one syllable; the .com, .org, .app, npm and "
        "GitHub org were all free. Porchlog is the runner-up."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "let me think"]'),
    (
        "OpenJournal is finished apart from its name — everything phases 1–4 called for is "
        "built, tested and running as two federating nodes on testhost "
        "(http://testhost:4747, password openjournal-demo). It now has an AGPL-3.0 "
        "licence and a full protocol spec, so the only thing standing between it and a public "
        "repo plus a domain is what to call it. You asked about 'Off-line' and it lost badly: "
        "offline.com is American Eagle's OFFLINE by Aerie brand, there's an Offline Social "
        "app on both stores and Offline Diary and Offline Journal on Play — but the real "
        "disqualifier is search, since 'offline' is one of the most common words in computing "
        "and the project would never rank for its own name anywhere. Peek and Noted lost the "
        "same way (peek.com is a funded travel company, phw/peek is a 10.5k-star GIF "
        "recorder, notedapp.io has a trademark claim). My pick is Kithlog — 'kith', as in "
        "kith and kin, the old word for exactly your circle of friends, which is the product "
        "thesis in one syllable; the .com, .org, .app, npm and the GitHub org were all free. "
        "Porchlog is the runner-up. This is the one call that gets more expensive the longer "
        "it waits: the hostname becomes your handle and your friends' nodes pin it, so "
        "renaming after a domain exists means re-introducing yourself to everyone."
        , '["Kithlog", "Porchlog", "keep OpenJournal", "let me think"]'),
]
