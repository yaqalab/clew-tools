// SHA-256 and HMAC-SHA-256 (FIPS 180-4, RFC 2104). Portable on purpose: the same code signs stream
// links on every system, and its test vectors are in test/unit_tests.cpp.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace clew {

using Digest = std::array<std::uint8_t, 32>;

class Sha256 {
public:
    Sha256();
    void update(const void* data, std::size_t len);
    Digest finish();

private:
    void block(const std::uint8_t* p);
    std::uint32_t h_[8];
    std::uint8_t buf_[64];
    std::size_t used_ = 0;
    std::uint64_t total_ = 0;
};

Digest sha256(const std::string& data);
Digest hmac_sha256(const std::string& key, const std::string& message);

// Compares without stopping at the first difference.
bool equal_constant_time(const std::string& a, const std::string& b);

}  // namespace clew
