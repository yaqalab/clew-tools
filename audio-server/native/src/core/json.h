// A small JSON value, reader and writer: enough for the settings file and the API's bodies.
// Writing escapes everything outside ASCII as \uXXXX, as Python's json.dumps does.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace clew::json {

class Value {
public:
    enum class Kind { Null, Bool, Number, String, Array, Object };

    Value() = default;
    Value(std::nullptr_t) {}
    Value(bool b) : kind_(Kind::Bool), bool_(b) {}
    Value(int n) : kind_(Kind::Number), number_(n) {}
    Value(long long n) : kind_(Kind::Number), number_(double(n)) {}
    Value(unsigned long long n) : kind_(Kind::Number), number_(double(n)) {}
    Value(double n) : kind_(Kind::Number), number_(n) {}
    Value(const char* s) : kind_(Kind::String), string_(s) {}
    Value(std::string s) : kind_(Kind::String), string_(std::move(s)) {}

    static Value array() {
        Value v;
        v.kind_ = Kind::Array;
        return v;
    }
    static Value object() {
        Value v;
        v.kind_ = Kind::Object;
        return v;
    }

    Kind kind() const { return kind_; }
    bool is_string() const { return kind_ == Kind::String; }
    bool is_number() const { return kind_ == Kind::Number; }
    bool is_bool() const { return kind_ == Kind::Bool; }
    bool is_array() const { return kind_ == Kind::Array; }
    bool is_object() const { return kind_ == Kind::Object; }

    const std::string& str() const { return string_; }
    double num() const { return number_; }
    bool boolean() const { return bool_; }
    const std::vector<Value>& items() const { return items_; }
    // Keys in insertion order, so a written file reads the way it was built.
    const std::vector<std::pair<std::string, Value>>& members() const { return members_; }

    // Object access; a missing key gives a null value.
    const Value& operator[](const std::string& key) const;
    Value& set(const std::string& key, Value v);
    Value& push(Value v);

    std::string dump(int indent = -1) const;

private:
    void write(std::string& out, int indent, int depth) const;

    Kind kind_ = Kind::Null;
    bool bool_ = false;
    double number_ = 0;
    std::string string_;
    std::vector<Value> items_;
    std::vector<std::pair<std::string, Value>> members_;
};

// Strict JSON; nesting deeper than 32 or invalid UTF-8 is refused.
std::optional<Value> parse(const std::string& text);

}  // namespace clew::json
