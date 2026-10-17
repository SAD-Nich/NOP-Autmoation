#include <iostream>
#define NOMINMAX
#include <windows.h>
#include <ctime>
#include <set>
#include <vector>
#include <algorithm>
#include <iomanip>
#include <map>
#include <sstream>
#include <cmath>
#include <fstream>
#include <cctype>

#pragma comment(lib, "ws2_32.lib")
#include "MT4ManagerAPI.h"
#include "sqlite3.h"

std::string TimestampToDateTime(time_t raw_time) {
    if (raw_time <= 0) return "NULL";
    struct tm dt;
    gmtime_s(&dt, &raw_time);
    char buffer[20];
    strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &dt);
    return std::string(buffer);
}

std::string GetCmdType(int cmd) {
    return cmd == 0 ? "BUY" : "SELL";
}

std::string GetOppositeCmdType(int cmd) {
    return cmd == 0 ? "SELL" : "BUY";
}

// =========================================================
// SQLite helpers
// =========================================================
sqlite3* OpenDatabase(const char* path) {
    sqlite3* db = nullptr;
    if (sqlite3_open(path, &db) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Gagal buka database: " << sqlite3_errmsg(db) << std::endl;
        return nullptr;
    }

    // Dua tabel dibuat sekaligus dalam satu sqlite3_exec, dipisah titik koma.
    const char* create_sql =
        // Tabel 1: history lengkap (kontinyu, N/L legs, upsert)
        "CREATE TABLE IF NOT EXISTS trades ("
        "  ticket      INTEGER NOT NULL,"
        "  leg         TEXT NOT NULL,"          // 'N' atau 'L'
        "  login       INTEGER NOT NULL,"
        "  time        TEXT NOT NULL,"
        "  type        TEXT NOT NULL,"
        "  symbol      TEXT NOT NULL,"
        "  lot         REAL NOT NULL,"
        "  open_price  REAL,"
        "  close_price REAL,"
        "  status      TEXT NOT NULL,"
        "  updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,"
        "  PRIMARY KEY (ticket, leg)"
        ");"
        // Tabel 2: snapshot posisi open SAAT INI SAJA - di-clear & ditulis ulang tiap run
        "CREATE TABLE IF NOT EXISTS open_positions_snapshot ("
        "  deal        INTEGER PRIMARY KEY,"
        "  login       INTEGER NOT NULL,"
        "  open_time   TEXT NOT NULL,"
        "  type        TEXT NOT NULL,"
        "  symbol      TEXT NOT NULL,"
        "  volume      REAL NOT NULL,"
        "  price       REAL NOT NULL"
        ");"
        // Tabel 3: cache login yang diketahui pernah punya history trading -
        // dipakai supaya sync periodik nggak perlu loop SEMUA akun di server
        // (misal 12.000 akun), cukup loop yang memang aktif saja.
        "CREATE TABLE IF NOT EXISTS active_logins ("
        "  login       INTEGER PRIMARY KEY,"
        "  last_seen   TEXT DEFAULT CURRENT_TIMESTAMP"
        ");";

    char* err_msg = nullptr;
    if (sqlite3_exec(db, create_sql, nullptr, nullptr, &err_msg) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Gagal buat tabel: " << err_msg << std::endl;
        sqlite3_free(err_msg);
        sqlite3_close(db);
        return nullptr;
    }

    return db;
}

struct OpenPositionRow {
    int deal;
    int login;
    std::string open_time;
    std::string type;
    std::string symbol;
    double volume;
    double price;
};

// Ambil daftar login yang sudah diketahui aktif dari cache (hasil full-scan sebelumnya).
std::vector<int> GetCachedActiveLogins(sqlite3* db) {
    std::vector<int> logins;
    sqlite3_stmt* stmt = nullptr;

    if (sqlite3_prepare_v2(db, "SELECT login FROM active_logins;", -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Gagal baca active_logins: " << sqlite3_errmsg(db) << std::endl;
        return logins;
    }

    while (sqlite3_step(stmt) == SQLITE_ROW) {
        logins.push_back(sqlite3_column_int(stmt, 0));
    }
    sqlite3_finalize(stmt);
    return logins;
}

// Tandai satu login sebagai aktif (upsert, update last_seen tiap kali dipanggil).
void MarkLoginActive(sqlite3* db, int login) {
    sqlite3_stmt* stmt = nullptr;
    const char* sql =
        "INSERT INTO active_logins (login, last_seen) VALUES (?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(login) DO UPDATE SET last_seen=CURRENT_TIMESTAMP;";

    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) return;
    sqlite3_bind_int(stmt, 1, login);
    sqlite3_step(stmt);
    sqlite3_finalize(stmt);
}

// Hapus semua isi open_positions_snapshot lalu tulis ulang dari data terbaru.
// CATATAN: fungsi ini TIDAK membungkus transaction sendiri - dipanggil dari
// dalam transaction besar yang sudah dibuka di main(), karena SQLite tidak
// support nested transaction.
void RefreshOpenPositionsSnapshot(sqlite3* db, const std::vector<OpenPositionRow>& rows) {
    char* err_msg = nullptr;

    if (sqlite3_exec(db, "DELETE FROM open_positions_snapshot;", nullptr, nullptr, &err_msg) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Clear snapshot gagal: " << err_msg << std::endl;
        sqlite3_free(err_msg);
        return;
    }

    const char* sql =
        "INSERT INTO open_positions_snapshot (deal, login, open_time, type, symbol, volume, price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?);";

    sqlite3_stmt* stmt = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Prepare snapshot gagal: " << sqlite3_errmsg(db) << std::endl;
        return;
    }

    for (const auto& row : rows) {
        sqlite3_bind_int(stmt, 1, row.deal);
        sqlite3_bind_int(stmt, 2, row.login);
        sqlite3_bind_text(stmt, 3, row.open_time.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 4, row.type.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 5, row.symbol.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_double(stmt, 6, row.volume);
        sqlite3_bind_double(stmt, 7, row.price);

        if (sqlite3_step(stmt) != SQLITE_DONE) {
            std::cerr << "[DB ERROR] Insert snapshot gagal untuk deal " << row.deal
                << ": " << sqlite3_errmsg(db) << std::endl;
        }
        sqlite3_reset(stmt);
    }

    sqlite3_finalize(stmt);

    std::cerr << "[DEBUG] open_positions_snapshot di-refresh, " << rows.size() << " baris ditulis." << std::endl;
}

// Upsert satu baris (leg N atau L)
void UpsertTradeLeg(sqlite3* db, int ticket, const char* leg, int login,
    const std::string& time_str, const std::string& type,
    const std::string& symbol, double lot,
    double open_price, bool has_close_price, double close_price,
    const std::string& status) {

    const char* sql =
        "INSERT INTO trades (ticket, leg, login, time, type, symbol, lot, open_price, close_price, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(ticket, leg) DO UPDATE SET "
        "  login=excluded.login, time=excluded.time, type=excluded.type, "
        "  symbol=excluded.symbol, lot=excluded.lot, open_price=excluded.open_price, "
        "  close_price=excluded.close_price, status=excluded.status, "
        "  updated_at=CURRENT_TIMESTAMP;";

    sqlite3_stmt* stmt = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
        std::cerr << "[DB ERROR] Prepare gagal: " << sqlite3_errmsg(db) << std::endl;
        return;
    }

    sqlite3_bind_int(stmt, 1, ticket);
    sqlite3_bind_text(stmt, 2, leg, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(stmt, 3, login);
    sqlite3_bind_text(stmt, 4, time_str.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 5, type.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 6, symbol.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_double(stmt, 7, lot);
    sqlite3_bind_double(stmt, 8, open_price);
    if (has_close_price) {
        sqlite3_bind_double(stmt, 9, close_price);
    } else {
        sqlite3_bind_null(stmt, 9);
    }
    sqlite3_bind_text(stmt, 10, status.c_str(), -1, SQLITE_TRANSIENT);

    if (sqlite3_step(stmt) != SQLITE_DONE) {
        std::cerr << "[DB ERROR] Insert/update gagal: " << sqlite3_errmsg(db) << std::endl;
    }

    sqlite3_finalize(stmt);
}

// =========================================================
// Print + Save (console tetap jalan, plus simpan ke DB)
// =========================================================
void HandleOpenLeg(sqlite3* db, int ticket_num, int login, time_t open_time, int cmd,
    LPCSTR symbol, double volume, double open_price) {

    std::string time_str = TimestampToDateTime(open_time);
    std::string type_str = GetCmdType(cmd);

    std::cout << ticket_num << "N | " << login << " | " << time_str << " | "
        << type_str << " | " << symbol << " | " << (volume / 100.0) << " | "
        << open_price << " | NULL | OPEN" << std::endl;

    UpsertTradeLeg(db, ticket_num, "N", login, time_str, type_str, symbol,
        volume / 100.0, open_price, false, 0.0, "OPEN");
}

void HandleCloseLeg(sqlite3* db, int ticket_num, int login, int cmd,
    LPCSTR symbol, double volume, time_t close_time,
    double open_price, double close_price) {

    std::string time_str = TimestampToDateTime(close_time);
    std::string type_str = GetOppositeCmdType(cmd);

    std::cout << ticket_num << "L | " << login << " | " << time_str << " | "
        << type_str << " | " << symbol << " | " << (volume / 100.0) << " | "
        << open_price << " | " << close_price << " | CLOSED" << std::endl;

    UpsertTradeLeg(db, ticket_num, "L", login, time_str, type_str, symbol,
        volume / 100.0, open_price, true, close_price, "CLOSED");
}


// =========================================================
// NOP + dashboard helpers
// =========================================================

// Convert raw MT4 symbols into the product names used in the NOP report.
// These groupings mirror the Google Sheet:
// HKK* -> HKK, JPK* -> JPK, CLS* -> OIL,
// DJC* -> DOW, NQC* -> NASDAQ, SILV* -> XAGUSD.
std::string GetNOPSymbol(const std::string& raw_symbol) {
    std::string symbol = raw_symbol;

    std::transform(symbol.begin(), symbol.end(), symbol.begin(),
        [](unsigned char c) { return static_cast<char>(std::toupper(c)); });

    if (symbol.rfind("JPK", 0) == 0)
        return "JPK";

    if (symbol.rfind("HKK", 0) == 0)
        return "HKK";

    if (symbol.rfind("CLS", 0) == 0)
        return "OIL";

    if (symbol.rfind("DJC", 0) == 0)
        return "DOW";

    if (symbol.rfind("NQC", 0) == 0)
        return "NASDAQ";

    if (symbol.rfind("SILV", 0) == 0)
        return "XAGUSD";

    return symbol;
}

std::string FormatNOPNumber(double value) {
    if (std::abs(value) < 0.000001)
        value = 0.0;

    std::ostringstream ss;
    ss << std::fixed << std::setprecision(2) << value;

    std::string result = ss.str();

    while (!result.empty() && result.back() == '0')
        result.pop_back();

    if (!result.empty() && result.back() == '.')
        result.pop_back();

    return result;
}

// Same MT4/server-time representation used by TimestampToDateTime().
std::string FormatNOPTime(time_t raw_time) {
    if (raw_time <= 0) return "NULL";

    struct tm dt;
    gmtime_s(&dt, &raw_time);

    char buffer[32];
    strftime(buffer, sizeof(buffer), "%d/%m/%Y %H:%M", &dt);
    return std::string(buffer);
}

std::map<std::string, double> CalculateNOP(
    const std::vector<OpenPositionRow>& rows)
{
    std::map<std::string, double> nop;

    for (const auto& row : rows) {
        std::string symbol = GetNOPSymbol(row.symbol);

        double signed_volume = 0.0;

        if (row.type == "BUY")
            signed_volume = row.volume;
        else if (row.type == "SELL")
            signed_volume = -row.volume;
        else
            continue;

        nop[symbol] += signed_volume;
    }

    return nop;
}


int GetLoginSuffix(int login) {
    std::string login_str = std::to_string(login);

    if (login_str.size() < 2)
        return login;

    return std::stoi(login_str.substr(0, 2));
}

// Exact "Multiply 14" mapping from the DB sheet.
bool Get14KMultiplier(int suffix, double& multiplier) {
    static const std::map<int, double> multipliers = {
        {50, 1.4},
        {51, 1.0},
        {52, 1.0},
        {53, 1.2},
        {56, 1.2},
        {57, 1.4},
        {74, 1.0},
        {76, 1.2},
        {77, 1.4},
        {80, 1.4},
        {81, 1.0},
        {82, 1.6},
        {83, 1.2},
        {84, 1.0},
        {85, 1.6},
        {86, 1.2},
        {87, 1.4},
        {89, 1.0},
        {13, 1.0}
    };

    auto it = multipliers.find(suffix);

    if (it == multipliers.end())
        return false;

    multiplier = it->second;
    return true;
}

// Google Sheet logic:
//
// Orders Report!X = first two digits of Login
// Orders Report!Y = XLOOKUP(Suffix, DB!Suffix, DB!Multiply 14)
// Orders Report!Z = signed Volume * Multiply 14
// Total NOP!B      = SUM(Z) / 1.4, grouped by raw symbol
//
// Since division distributes over the sum, this implementation
// accumulates each signed position as:
//     signed lot * multiplier / 1.4
//
// before grouping it into the final canonical product name.
std::map<std::string, double> CalculateNOP14K(
    const std::vector<OpenPositionRow>& rows,
    std::set<int>& unmapped_suffixes)
{
    std::map<std::string, double> nop14;

    for (const auto& row : rows) {
        double signed_volume = 0.0;

        if (row.type == "BUY")
            signed_volume = row.volume;
        else if (row.type == "SELL")
            signed_volume = -row.volume;
        else
            continue;

        int suffix = GetLoginSuffix(row.login);
        double multiplier = 0.0;

        if (!Get14KMultiplier(suffix, multiplier)) {
            unmapped_suffixes.insert(suffix);
            continue;
        }

        std::string symbol = GetNOPSymbol(row.symbol);
        nop14[symbol] += (signed_volume * multiplier) / 1.4;
    }

    return nop14;
}

bool IsSingleStockSymbol(const std::string& symbol) {
    return !symbol.empty() && symbol[0] == '#';
}

std::vector<std::string> GetPreferredSPAOrder() {
    return {
        "EURUSD",
        "USDJPY",
        "GBPUSD",
        "USDCHF",
        "AUDUSD",
        "XAUUSD",
        "HKK",
        "JPK",
        "OIL",
        "DOW",
        "NASDAQ",
        "XAGUSD",
        "EURJPY",
        "GBPJPY",
        "CHFJPY",
        "NZDUSD",
        "USDCAD"
    };
}

std::vector<std::pair<std::string, double>> GetOrderedSPARows(
    const std::map<std::string, double>& nop)
{
    std::vector<std::pair<std::string, double>> result;
    std::set<std::string> added;

    for (const auto& symbol : GetPreferredSPAOrder()) {
        auto it = nop.find(symbol);
        if (it != nop.end() && !IsSingleStockSymbol(it->first)) {
            result.push_back(*it);
            added.insert(it->first);
        }
    }

    // Still display any unexpected non-stock symbol so exposure is not hidden.
    for (const auto& item : nop) {
        if (!IsSingleStockSymbol(item.first) &&
            added.find(item.first) == added.end()) {
            result.push_back(item);
        }
    }

    return result;
}

std::vector<std::pair<std::string, double>> GetOrderedSPA14KRows(
    const std::map<std::string, double>& nop)
{
    std::vector<std::pair<std::string, double>> result;
    std::set<std::string> added;

    for (const auto& symbol : GetPreferredSPAOrder()) {
        auto it = nop.find(symbol);

        if (it != nop.end() &&
            !IsSingleStockSymbol(it->first) &&
            std::abs(it->second) >= 0.000001) {
            result.push_back(*it);
            added.insert(it->first);
        }
    }

    for (const auto& item : nop) {
        if (!IsSingleStockSymbol(item.first) &&
            added.find(item.first) == added.end() &&
            std::abs(item.second) >= 0.000001) {
            result.push_back(item);
        }
    }

    return result;
}

std::vector<std::pair<std::string, double>> GetSingleStockRows(
    const std::map<std::string, double>& nop)
{
    std::vector<std::pair<std::string, double>> result;

    for (const auto& item : nop) {
        if (IsSingleStockSymbol(item.first))
            result.push_back(item);
    }

    return result;
}

void PrintNOPSummary(
    const std::vector<OpenPositionRow>& rows,
    time_t server_time)
{
    const auto nop = CalculateNOP(rows);
    const auto spa_rows = GetOrderedSPARows(nop);
    const auto stock_rows = GetSingleStockRows(nop);

    std::set<int> unmapped_14k_suffixes;
    const auto nop14 = CalculateNOP14K(rows, unmapped_14k_suffixes);
    const auto spa14_rows = GetOrderedSPA14KRows(nop14);

    std::cout << "\n\n";
    std::cout << "======================================================================\n";
    std::cout << "                              Content NOP\n";
    std::cout << "                       NOP TIME " << FormatNOPTime(server_time) << "\n";
    std::cout << "======================================================================\n";
    std::cout << "Client SPA          Total NOP ( Reguler / Mini / Lumen ) :\n\n";

    for (const auto& item : spa_rows) {
        std::cout
            << std::left << std::setw(12) << item.first
            << " = "
            << std::right << std::setw(8) << FormatNOPNumber(item.second)
            << " ( "
            << FormatNOPNumber(item.second)
            << " / 0 / 0 )\n";
    }

    std::cout << "\n-----------------------\n";
    std::cout << "Client US Single Stock\n";
    std::cout << "Total NOP ( Reguler / Mini ) :\n\n";

    for (const auto& item : stock_rows) {
        std::cout
            << std::left << std::setw(12) << item.first
            << " = "
            << std::right << std::setw(8) << FormatNOPNumber(item.second)
            << " ( "
            << FormatNOPNumber(item.second)
            << " / 0 )\n";
    }

    std::cout << "-----------------------\n\n";

    std::cout << "NOP KONVERSI 14 RIBU\n";
    std::cout << "Client SPA\n\n";

    for (const auto& item : spa14_rows) {
        std::cout
            << std::left << std::setw(12) << item.first
            << " = "
            << std::right << std::setw(8) << FormatNOPNumber(item.second)
            << "\n";
    }

    std::cout << "-----------------------\n";

    if (!unmapped_14k_suffixes.empty()) {
        std::cerr << "[NOP14 WARNING] Suffix login belum ada di mapping DB:";
        for (int suffix : unmapped_14k_suffixes)
            std::cerr << " " << suffix;
        std::cerr << std::endl;
    }

    std::cout << "======================================================================\n";
}

std::string EscapeJson(const std::string& value) {
    std::string out;
    out.reserve(value.size());

    for (char c : value) {
        switch (c) {
        case '\\': out += "\\\\"; break;
        case '"':  out += "\\\""; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default: out += c; break;
        }
    }

    return out;
}

// Writes the verified C++ NOP result to a local JSON file.
// The browser UI reads this file through nop_dashboard.py.
void WriteNOPSnapshotJson(
    const std::vector<OpenPositionRow>& rows,
    time_t server_time,
    const std::string& output_path)
{
    const auto nop = CalculateNOP(rows);
    const auto spa_rows = GetOrderedSPARows(nop);
    const auto stock_rows = GetSingleStockRows(nop);

    std::set<int> unmapped_14k_suffixes;
    const auto nop14 = CalculateNOP14K(rows, unmapped_14k_suffixes);
    const auto spa14_rows = GetOrderedSPA14KRows(nop14);

    std::ofstream out(output_path, std::ios::out | std::ios::trunc);

    if (!out.is_open()) {
        std::cerr << "[JSON ERROR] Gagal membuat " << output_path << std::endl;
        return;
    }

    out << "{\n";
    out << "  \"nop_time\": \"" << EscapeJson(FormatNOPTime(server_time)) << "\",\n";

    out << "  \"spa\": [\n";
    for (size_t i = 0; i < spa_rows.size(); i++) {
        out << "    {\"symbol\": \"" << EscapeJson(spa_rows[i].first)
            << "\", \"total\": " << FormatNOPNumber(spa_rows[i].second) << "}";

        if (i + 1 < spa_rows.size())
            out << ",";

        out << "\n";
    }
    out << "  ],\n";

    out << "  \"stocks\": [\n";
    for (size_t i = 0; i < stock_rows.size(); i++) {
        out << "    {\"symbol\": \"" << EscapeJson(stock_rows[i].first)
            << "\", \"total\": " << FormatNOPNumber(stock_rows[i].second) << "}";

        if (i + 1 < stock_rows.size())
            out << ",";

        out << "\n";
    }
    out << "  ],\n";

    out << "  \"spa14\": [\n";
    for (size_t i = 0; i < spa14_rows.size(); i++) {
        out << "    {\"symbol\": \"" << EscapeJson(spa14_rows[i].first)
            << "\", \"total\": " << FormatNOPNumber(spa14_rows[i].second) << "}";

        if (i + 1 < spa14_rows.size())
            out << ",";

        out << "\n";
    }
    out << "  ],\n";

    out << "  \"unmapped_14k_suffixes\": [";
    size_t suffix_index = 0;
    for (int suffix : unmapped_14k_suffixes) {
        if (suffix_index++ > 0)
            out << ", ";
        out << suffix;
    }
    out << "]\n";

    out << "}\n";

    out.close();

    std::cerr << "[DEBUG] NOP JSON dibuat: " << output_path << std::endl;
}

int main(int argc, char* argv[]) {
    // Mode "full": scan SEMUA login di server (lambat, untuk sekali di awal
    // atau dijadwalkan jarang, misal 1x sehari) - dipicu dengan argumen "full".
    // Mode default ("quick"): cuma loop login yang sudah tercatat di
    // active_logins (hasil dari full-scan sebelumnya) - jauh lebih cepat,
    // dipakai untuk sync periodik tiap 15 menit.
    bool full_scan = false;
    bool no_pause = false;
    bool nop_only = false;

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];

        if (arg == "full")
            full_scan = true;
        else if (arg == "--no-pause")
            no_pause = true;
        else if (arg == "--nop-only")
            nop_only = true;
    }

    sqlite3* db = OpenDatabase("trades.db");
    if (db == nullptr) return -1;

    CManagerFactory factory;
    factory.Init("mtmanapi64.dll");

    if (factory.WinsockStartup() != RET_OK) {
        std::cerr << "Gagal menyalakan Winsock (Jaringan)." << std::endl;
        sqlite3_close(db);
        return -1;
    }

    CManagerInterface* manager = factory.Create(ManAPIVersion);
    if (manager == nullptr) {
        std::cerr << "Gagal membuat Manager Interface." << std::endl;
        factory.WinsockCleanup();
        sqlite3_close(db);
        return -1;
    }

    int connect_result = manager->Connect("mt4live.agrodana-futures.com");
    std::cerr << "[DEBUG] Connect() return code = " << connect_result << std::endl;
    if (connect_result != RET_OK) {
        std::cerr << "Gagal terkoneksi ke server." << std::endl;
        manager->Release();
        factory.WinsockCleanup();
        sqlite3_close(db);
        if (!no_pause) {
            std::cout << "\nTekan Enter untuk keluar...";
            std::cin.get();
        }
        return -1;
    }

    int login_result = manager->Login(1101, "Top1234Up");
    std::cerr << "[DEBUG] Login() return code = " << login_result << std::endl;
    if (login_result != RET_OK) {
        std::cerr << "Login gagal. Cek kredensial." << std::endl;
        manager->Disconnect();
        manager->Release();
        factory.WinsockCleanup();
        sqlite3_close(db);
        if (!no_pause) {
            std::cout << "\nTekan Enter untuk keluar...";
            std::cin.get();
        }
        return -1;
    }

    // Login yang TIDAK mau ditarik datanya - tambahkan sesuai kebutuhan
    std::set<int> exclude_logins = {
        // 123456,
        // 789012,
    };

    // Batasi history ke N JAM terakhir dari sekarang (rolling window) -
    // jauh lebih ringan dibanding narik berhari-hari, cocok untuk sync
    // periodik tiap 15 menit. Angka jam dilebihkan dikit dari interval
    // sync-nya (misal sync tiap 15 menit tapi window 1 jam) supaya ada
    // buffer kalau ada run yang sempat telat/gagal.
  const int HISTORY_MINUTES_BACK = 60;

    __time32_t now = manager->ServerTime();
    int time_from = (int)(now - (HISTORY_MINUTES_BACK * 60)); // 15 * 60 detik (15 menit)
    int time_to = (int)now;

    std::cout << "\nTicket | Login |  Time | Type | Product | Lot | Open Price | Close Price | Status" << std::endl;
    std::cout << "--------------------------------------------------------------------------------------------------------" << std::endl;

    std::set<int> open_tickets;
    std::set<int> printed_tickets;
    std::vector<OpenPositionRow> snapshot_rows; // untuk tabel open_positions_snapshot

    // Bungkus SEMUA insert/update ke tabel trades dalam 1 transaction besar.
    // Tanpa ini, tiap 1 INSERT langsung di-commit ke disk sendiri-sendiri -
    // sangat lambat kalau jumlahnya ribuan. Dengan transaction, semuanya
    // ditulis sekaligus di akhir - jauh lebih cepat.
    sqlite3_exec(db, "BEGIN TRANSACTION;", nullptr, nullptr, nullptr);

    // A. Floating (OPEN) - dengan retry + BATCHING
    // Server dengan banyak order live (ratusan ribu) bisa bikin
    // TradeRecordsRequest() gagal/putus koneksi kalau semua order ID
    // dikirim sekaligus dalam 1 request. Jadi kita pecah jadi beberapa
    // batch kecil.
    const int MAX_RETRY = 3;
    const int BATCH_SIZE = 2000; // aman untuk request ke server manapun

    for (int attempt = 1; attempt <= MAX_RETRY; attempt++) {
        int total_snapshot = 0;
        int* order_snapshot_ids = manager->TradesSnapshot(&total_snapshot);
        bool got_any = false;

        std::cerr << "[DEBUG] Percobaan #" << attempt << " TradesSnapshot -> total_snapshot="
            << total_snapshot << std::endl;

        if (order_snapshot_ids != nullptr && total_snapshot > 0) {
            int total_batches = (total_snapshot + BATCH_SIZE - 1) / BATCH_SIZE;
            int batch_num = 0;

            // Proses per-batch, bukan sekaligus semua total_snapshot
            for (int offset = 0; offset < total_snapshot; offset += BATCH_SIZE) {
                batch_num++;
                int batch_count = std::min(BATCH_SIZE, total_snapshot - offset);

                std::cerr << "[PROGRESS] Batch " << batch_num << "/" << total_batches
                    << " (order " << offset << "-" << (offset + batch_count) << ")" << std::endl;

                TradeRecord* open_trades = manager->TradeRecordsRequest(
                    order_snapshot_ids + offset, &batch_count);

                if (open_trades == nullptr) {
                    std::cerr << "[DEBUG] Batch offset=" << offset
                        << " gagal (NULL) - lanjut ke batch berikutnya" << std::endl;
                    continue; // jangan berhenti total, lanjut batch lain
                }

                for (int i = 0; i < batch_count; i++) {
                    if (exclude_logins.find(open_trades[i].login) == exclude_logins.end() &&
                        open_trades[i].close_time == 0 &&
                        (open_trades[i].cmd == 0 || open_trades[i].cmd == 1)) {

                        int ticket_num = open_trades[i].order;
                        if (open_tickets.find(ticket_num) == open_tickets.end()) {
                            open_tickets.insert(ticket_num);
                            got_any = true;
                            MarkLoginActive(db, open_trades[i].login);
                            HandleOpenLeg(db, ticket_num, open_trades[i].login,
                                open_trades[i].open_time, open_trades[i].cmd,
                                open_trades[i].symbol, open_trades[i].volume,
                                open_trades[i].open_price);

                            // Data untuk tabel snapshot (Deal|Login|Open Time|Type|Symbol|Volume|Price)
                            OpenPositionRow row;
                            row.deal = ticket_num;
                            row.login = open_trades[i].login;
                            row.open_time = TimestampToDateTime(open_trades[i].open_time);
                            row.type = GetCmdType(open_trades[i].cmd);
                            row.symbol = open_trades[i].symbol;
                            row.volume = open_trades[i].volume / 100.0;
                            row.price = open_trades[i].open_price;
                            snapshot_rows.push_back(row);
                        }
                    }
                }
                manager->MemFree(open_trades);
            }
            manager->MemFree(order_snapshot_ids);
        }

        if (got_any || attempt == MAX_RETRY) break;
        Sleep(1000);
    }

    // Clear tabel snapshot lalu tulis ulang dengan data open position terbaru.
    // Kalau snapshot_rows kosong (nggak ada posisi open sama sekali), tabel
    // akan jadi kosong juga - ini memang perilaku yang diminta.
    RefreshOpenPositionsSnapshot(db, snapshot_rows);

    // Calculate CURRENT NOP from the current OPEN-position snapshot.
    // No closed/liquidated trade is included in this NOP calculation.
    time_t nop_server_time = (time_t)manager->ServerTime();
    PrintNOPSummary(snapshot_rows, nop_server_time);
    WriteNOPSnapshotJson(snapshot_rows, nop_server_time, "nop_latest.json");

    // When called from the local dashboard, current NOP is all we need.
    // Commit the local DB changes and skip the slower closed-history scan.
    if (nop_only) {
        sqlite3_exec(db, "COMMIT;", nullptr, nullptr, nullptr);
        std::cerr << "[DEBUG] NOP-only transaction committed." << std::endl;

        manager->Disconnect();
        manager->Release();
        factory.WinsockCleanup();
        sqlite3_close(db);

        std::cout << "\nNOP snapshot tersimpan ke nop_latest.json" << std::endl;

        if (!no_pause) {
            std::cout << "\nSelesai. Tekan Enter untuk keluar...";
            std::cin.get();
        }

        return 0;
    }

    // B. Closed (history) - N + L
    // Mode full: ambil daftar login dari UsersRequest (SEMUA akun di server).
    // Mode quick: ambil daftar login dari cache active_logins saja.
    std::vector<int> logins_to_check;
    UserRecord* all_users = nullptr;

    if (full_scan) {
        int total_users = 0;
        all_users = manager->UsersRequest(&total_users);

        std::cerr << "[DEBUG] FULL SCAN - UsersRequest -> total_users=" << total_users
            << " ptr=" << (all_users ? "OK" : "NULL") << std::endl;

        if (all_users != nullptr && total_users > 0) {
            for (int u = 0; u < total_users; u++) {
                logins_to_check.push_back(all_users[u].login);
            }
        }
    } else {
        logins_to_check = GetCachedActiveLogins(db);
        std::cerr << "[DEBUG] QUICK SYNC - " << logins_to_check.size()
            << " login dari cache active_logins" << std::endl;
    }

    for (int login : logins_to_check) {
        // Skip login yang ada di exclude list
        if (exclude_logins.find(login) != exclude_logins.end()) continue;

        int total_history = 0;
        TradeRecord* history_trades = manager->TradesUserHistory(login, time_from, time_to, &total_history);

        if (history_trades != nullptr && total_history > 0) {
            // Login ini terbukti punya history -> catat/refresh di cache
            // (di mode full ini yang membangun daftar active_logins dari nol;
            //  di mode quick ini cuma refresh last_seen untuk login yang sudah ada)
            MarkLoginActive(db, login);

            for (int i = 0; i < total_history; i++) {
                // Cuma proses transaksi BUY/SELL yang beneran tereksekusi (cmd 0/1).
                // Skip balance/credit ops (6/7) DAN skip pending order yang
                // cuma di-cancel/expired (cmd 2-5: limit/stop orders) - itu
                // bukan transaksi beneran, jangan ikut kehitung sebagai trade.
                if (history_trades[i].cmd != 0 && history_trades[i].cmd != 1) continue;

                int ticket_num = history_trades[i].order;
                if (open_tickets.find(ticket_num) != open_tickets.end()) continue;

                if (printed_tickets.find(ticket_num) == printed_tickets.end()) {
                    printed_tickets.insert(ticket_num);

                    HandleOpenLeg(db, ticket_num, history_trades[i].login,
                        history_trades[i].open_time, history_trades[i].cmd,
                        history_trades[i].symbol, history_trades[i].volume,
                        history_trades[i].open_price);

                    HandleCloseLeg(db, ticket_num, history_trades[i].login, history_trades[i].cmd,
                        history_trades[i].symbol, history_trades[i].volume,
                        history_trades[i].close_time,
                        history_trades[i].open_price, history_trades[i].close_price);
                }
            }
        }
        if (history_trades != nullptr) manager->MemFree(history_trades);
    }

    if (all_users != nullptr) manager->MemFree(all_users);

    // Commit semua insert/update sekaligus di sini.
    sqlite3_exec(db, "COMMIT;", nullptr, nullptr, nullptr);
    std::cerr << "[DEBUG] Transaction committed." << std::endl;

    manager->Disconnect();
    manager->Release();
    factory.WinsockCleanup();
    sqlite3_close(db);

    std::cout << "\nData tersimpan ke trades.db" << std::endl;

    if (!no_pause) {
        std::cout << "\nSelesai. Tekan Enter untuk keluar...";
        std::cin.get();
    }

    return 0;
}