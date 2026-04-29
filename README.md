# vetmarket-cli

CLI לניתוח החשבון שלך ב-vetmarket.co.il — חיפוש מוצרים, הזמנות, חשבוניות, מחירים, מגמות, התראות.

## התקנה
```bash
cd ~/vetmarket-cli
python -m pip install -r requirements.txt
```

הפרטים בקובץ `~/.clinic-secrets/vetmarket.env`:
```
VETMARKET_USERNAME=vet_batyam@yahoo.com
VETMARKET_PASSWORD=...
VETMARKET_BASE_URL=https://www.vetmarket.co.il
```

## הרצה
```bash
cd ~/vetmarket-cli
python -m vetmarket --help        # רשימת כל הפקודות
python -m vetmarket login         # בדיקת לוגין
python -m vetmarket sync all      # סנכרון מלא ל-DB מקומי
python -m vetmarket sync all --invoice-detail   # כולל פירוט שורות חשבונית (איטי יותר)
```

או דרך ה-wrapper: `./vetmarket.sh <command>`

## פקודות עיקריות

### סנכרון
| פקודה | תיאור |
|---|---|
| `sync all` | סנכרון כל הסקציות |
| `sync all --invoice-detail` | + פירוט מחירים מכל חשבונית |
| `sync products` | רק רשימת המוצרים העיקריים שלי |
| `sync favorites` | מוצרים מועדפים |
| `sync orders` | סטטוס הזמנות + פתיחת חוב |
| `sync invoices [--detail]` | חשבוניות (עם פירוט אם `--detail`) |
| `sync purchases` | סיכום רכישות (אגרגציה לפי מוצר) |
| `sync shipping` | תעודות משלוח |
| `sync offers` | הצעות מחיר פעילות |

### חיפוש ומוצר
| פקודה | תיאור |
|---|---|
| `search "סימפ"` | חיפוש לפי שם/מק"ט |
| `product 122286` | פרטי מוצר + סיכום מחירים + 10 מחירים אחרונים |

### הזמנות וחשבוניות
| פקודה | תיאור |
|---|---|
| `orders [--open] [-n N]` | רשימת הזמנות (כל / רק פתוחות) |
| `order 5260072273` | פירוט הזמנה |
| `invoices [-n N]` | רשימת חשבוניות |
| `invoice SI26004862` | פירוט שורות חשבונית |

### מחירים
| פקודה | תיאור |
|---|---|
| `prices list` | מחירון אישי מלא (מחיר אחרון לכל מוצר) |
| `prices show 122286` | היסטוריה מלאה למוצר |
| `prices trend 122286` | מגמה כרונולוגית עם ∆% בין חשבוניות |
| `prices anomalies [-t 15]` | שינויי מחיר ≥ סף (default 15%) |

### אנליטיקס
| פקודה | תיאור |
|---|---|
| `spend month [-m N]` | הוצאה חודשית |
| `spend year` | הוצאה שנתית |
| `top --by spend` | Top SKUs לפי הוצאה |
| `top --by qty` | Top SKUs לפי כמות |
| `status` | סטטוס DB + סנכרונים אחרונים |

### גנרי
- `--json` על כל פקודה → פלט JSON (לאינטגרציה עם תוכנות אחרות)
- `--help` על כל פקודה / תת-פקודה

## מבנה
```
vetmarket-cli/
├── vetmarket/
│   ├── config.py     # קונפיג + paths
│   ├── client.py     # HTTP client (ASP.NET WebForms session)
│   ├── parsers.py    # HTML → dicts
│   ├── db.py         # SQLite schema
│   ├── sync.py       # אורקסטרציית סנכרון
│   ├── reports.py    # שאילתות אנליטיקס
│   └── cli.py        # פקודות Typer
├── data/
│   ├── vetmarket.db  # SQLite
│   ├── session.json  # cookies (auto)
│   └── html/         # cache (debug)
└── requirements.txt
```

## הערות חשובות
- **מחירים נוכחיים אינם זמינים באתר** (לא בקטלוג, לא בסל). המחירים נקראים מהחשבוניות בדיעבד.
- מסד מחירים נבנה משני מקורות:
  - `invoice` — מחיר ליחידה אמיתי מתוך חשבונית רשמית
  - `purchases-avg` — מחיר ממוצע מחושב מסיכום רכישות (סה"כ ₪ ÷ סה"כ יח')
- ההפרדה בשדה `source` של טבלת `prices`.
