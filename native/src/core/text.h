// Small text helpers: base64, percent-encoding, query strings, ASCII case.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>

namespace clew {

std::string base64_encode(const std::string& bytes);
std::optional<std::string> base64_decode(const std::string& text);
// URL-safe alphabet, no padding (what Python's urlsafe_b64encode(...).rstrip("=") gives).
std::string base64url_encode(const std::string& bytes);

// Like Python's quote(s, safe=""): everything but letters, digits and "_.-~" is %XX.
std::string percent_encode(const std::string& s);
// Like Python's unquote_plus: "+" is a space, a broken %XX is kept as it is.
std::string percent_decode(const std::string& s);
// The first value of each key, as parse_qs gives.
std::map<std::string, std::string> parse_query(const std::string& query);

std::string to_lower(std::string s);
std::string trim(const std::string& s);
bool starts_with(const std::string& s, const std::string& prefix);
bool all_digits(const std::string& s);

// A valid, shortest-form UTF-8 string (no lone surrogates).
bool is_utf8(const std::string& s);

}  // namespace clew
