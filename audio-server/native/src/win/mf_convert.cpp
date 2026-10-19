// Conversion with Windows' own codecs (Media Foundation): any audio Windows can decode (WMA, WMA
// Voice, MP3, AAC, FLAC, Opus, AMR-NB on Windows 10/11) to a 128 kbit/s constant-bit-rate mp3.
// The encoder's raw frames are written as they come, so the result has no Xing/Info table and no
// ID3 tag: exactly what Firefox seeks well in (docs/22 section 8). Runs in the sandboxed child
// process only (see isolate.cpp), never in the server.
#include "mf_convert.h"

#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>
#include <wmcodecdsp.h>
#include <wrl/client.h>

#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace clew::win {

namespace {

constexpr UINT32 kBytesPerSecond = 128000 / 8;

struct Fail {
    std::string why;
};

void check(HRESULT hr, const char* what) {
    if (FAILED(hr)) {
        char buf[96];
        sprintf_s(buf, "%s (0x%08lx)", what, static_cast<unsigned long>(hr));
        throw Fail{buf};
    }
}

ComPtr<IMFMediaType> pcm_type(UINT32 rate, UINT32 channels) {
    ComPtr<IMFMediaType> t;
    check(MFCreateMediaType(&t), "media type");
    t->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
    t->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
    t->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
    t->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, rate);
    t->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, channels);
    t->SetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT, channels * 2);
    t->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, rate * channels * 2);
    t->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    return t;
}

class Writer {
public:
    explicit Writer(HANDLE out) : out_(out) {}
    void write(IMFSample* sample) {
        ComPtr<IMFMediaBuffer> buf;
        check(sample->ConvertToContiguousBuffer(&buf), "output buffer");
        BYTE* data = nullptr;
        DWORD len = 0;
        check(buf->Lock(&data, nullptr, &len), "lock");
        DWORD done = 0;
        BOOL ok = len == 0 || WriteFile(out_, data, len, &done, nullptr);
        buf->Unlock();
        if (!ok || done != len) throw Fail{"the output was closed"};
        written_ += len;
    }
    unsigned long long written() const { return written_; }

private:
    HANDLE out_;
    unsigned long long written_ = 0;
};

// Pulls everything a transform has ready, passing each sample on.
template <typename Sink>
void pull(IMFTransform* t, Sink&& sink) {
    MFT_OUTPUT_STREAM_INFO info{};
    check(t->GetOutputStreamInfo(0, &info), "output info");
    bool provides = (info.dwFlags & (MFT_OUTPUT_STREAM_PROVIDES_SAMPLES | MFT_OUTPUT_STREAM_CAN_PROVIDE_SAMPLES)) != 0;
    DWORD size = info.cbSize ? info.cbSize : 1 << 16;
    while (true) {
        MFT_OUTPUT_DATA_BUFFER out{};
        ComPtr<IMFSample> mine;
        if (!provides) {
            ComPtr<IMFMediaBuffer> buf;
            check(MFCreateSample(&mine), "sample");
            check(MFCreateMemoryBuffer(size, &buf), "buffer");
            mine->AddBuffer(buf.Get());
            out.pSample = mine.Get();
        }
        DWORD status = 0;
        HRESULT hr = t->ProcessOutput(0, 1, &out, &status);
        if (out.pEvents) out.pEvents->Release();
        if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) return;
        if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
            // Take the transform's own new output type and go on.
            ComPtr<IMFMediaType> type;
            check(t->GetOutputAvailableType(0, 0, &type), "new output type");
            check(t->SetOutputType(0, type.Get(), 0), "set new output type");
            continue;
        }
        check(hr, "process output");
        if (out.pSample) {
            sink(out.pSample);
            if (provides) out.pSample->Release();
        }
    }
}

// Feeds one sample, making room first when the transform is full.
template <typename Sink>
void push(IMFTransform* t, IMFSample* sample, Sink&& sink) {
    HRESULT hr = t->ProcessInput(0, sample, 0);
    if (hr == MF_E_NOTACCEPTING) {
        pull(t, sink);
        hr = t->ProcessInput(0, sample, 0);
    }
    check(hr, "process input");
    pull(t, sink);
}

template <typename Sink>
void finish(IMFTransform* t, Sink&& sink) {
    t->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
    t->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
    pull(t, sink);
}

void begin(IMFTransform* t) {
    t->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    t->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
}

// Windows' mp3 encoder takes only one of the output types it offers (each carries its own
// MPEGLAYER3WAVEFORMAT), and its output type before its input type. So: the offered type that
// matches the rate, then the channels, then is closest to 128 kbit/s; `rate` and `channels` come
// back as what was taken, and the decoded sound is resampled to them when they differ.
ComPtr<IMFTransform> mp3_encoder(UINT32* rate, UINT32* channels) {
    MFT_REGISTER_TYPE_INFO want{MFMediaType_Audio, MFAudioFormat_MP3};
    IMFActivate** acts = nullptr;
    UINT32 n = 0;
    check(MFTEnumEx(MFT_CATEGORY_AUDIO_ENCODER, MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER, nullptr, &want,
                    &acts, &n),
          "find the mp3 encoder");
    ComPtr<IMFTransform> enc;
    for (UINT32 i = 0; i < n; ++i) {
        if (!enc) acts[i]->ActivateObject(IID_PPV_ARGS(&enc));
        acts[i]->Release();
    }
    CoTaskMemFree(acts);
    if (!enc) throw Fail{"Windows has no mp3 encoder"};

    ComPtr<IMFMediaType> best;
    long long best_score = -1;
    for (DWORD i = 0;; ++i) {
        ComPtr<IMFMediaType> t;
        if (FAILED(enc->GetOutputAvailableType(0, i, &t))) break;
        UINT32 r = MFGetAttributeUINT32(t.Get(), MF_MT_AUDIO_SAMPLES_PER_SECOND, 0);
        UINT32 c = MFGetAttributeUINT32(t.Get(), MF_MT_AUDIO_NUM_CHANNELS, 0);
        UINT32 bps = MFGetAttributeUINT32(t.Get(), MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 0);
        UINT32 gap = bps > kBytesPerSecond ? bps - kBytesPerSecond : kBytesPerSecond - bps;
        long long score = (r == *rate ? 4000000LL : 0) + (r == 44100 ? 2000000LL : 0) + (c == *channels ? 1000000LL : 0) +
                          (1000000LL - gap);
        if (score > best_score) {
            best_score = score;
            best = t;
        }
    }
    if (!best) throw Fail{"the mp3 encoder offers no output"};
    check(enc->SetOutputType(0, best.Get(), 0), "mp3 encoder output");
    *rate = MFGetAttributeUINT32(best.Get(), MF_MT_AUDIO_SAMPLES_PER_SECOND, *rate);
    *channels = MFGetAttributeUINT32(best.Get(), MF_MT_AUDIO_NUM_CHANNELS, *channels);
    check(enc->SetInputType(0, pcm_type(*rate, *channels).Get(), 0), "mp3 encoder input");
    return enc;
}

ComPtr<IMFTransform> resampler(IMFMediaType* in, UINT32 rate, UINT32 channels) {
    ComPtr<IUnknown> unk;
    check(CoCreateInstance(CLSID_CResamplerMediaObject, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&unk)),
          "resampler");
    ComPtr<IMFTransform> t;
    check(unk.As(&t), "resampler transform");
    ComPtr<IWMResamplerProps> props;
    if (SUCCEEDED(unk.As(&props))) props->SetHalfFilterLength(60);  // best quality
    check(t->SetInputType(0, in, 0), "resampler input");
    check(t->SetOutputType(0, pcm_type(rate, channels).Get(), 0), "resampler output");
    return t;
}

void run(const wchar_t* in, HANDLE out) {
    ComPtr<IMFAttributes> attrs;
    check(MFCreateAttributes(&attrs, 1), "attributes");
    ComPtr<IMFSourceReader> reader;
    check(MFCreateSourceReaderFromURL(in, attrs.Get(), &reader), "Windows cannot read this file");
    const DWORD audio = static_cast<DWORD>(MF_SOURCE_READER_FIRST_AUDIO_STREAM);
    reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE);
    check(reader->SetStreamSelection(audio, TRUE), "no sound in this file");

    ComPtr<IMFMediaType> native;
    check(reader->GetNativeMediaType(audio, 0, &native), "no sound in this file");
    UINT32 rate = MFGetAttributeUINT32(native.Get(), MF_MT_AUDIO_SAMPLES_PER_SECOND, 44100);
    UINT32 channels = MFGetAttributeUINT32(native.Get(), MF_MT_AUDIO_NUM_CHANNELS, 2);
    // MPEG-1 layer III: 32, 44.1 or 48 kHz, one or two channels.
    UINT32 out_rate = (rate == 32000 || rate == 44100 || rate == 48000) ? rate : 44100;
    UINT32 out_channels = channels >= 2 ? 2 : 1;

    // Decoded to 16-bit PCM at the file's own rate and channels; resampled when needed.
    ComPtr<IMFMediaType> decoded;
    check(MFCreateMediaType(&decoded), "media type");
    decoded->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
    decoded->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
    decoded->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
    check(reader->SetCurrentMediaType(audio, nullptr, decoded.Get()), "Windows has no decoder for this file");
    ComPtr<IMFMediaType> current;
    check(reader->GetCurrentMediaType(audio, &current), "decoded type");
    rate = MFGetAttributeUINT32(current.Get(), MF_MT_AUDIO_SAMPLES_PER_SECOND, rate);
    channels = MFGetAttributeUINT32(current.Get(), MF_MT_AUDIO_NUM_CHANNELS, channels);

    auto enc = mp3_encoder(&out_rate, &out_channels);
    ComPtr<IMFTransform> rs;
    if (rate != out_rate || channels != out_channels) rs = resampler(current.Get(), out_rate, out_channels);

    Writer writer(out);
    auto to_file = [&](IMFSample* s) { writer.write(s); };
    auto to_encoder = [&](IMFSample* s) { push(enc.Get(), s, to_file); };
    if (rs) begin(rs.Get());
    begin(enc.Get());
    while (true) {
        DWORD flags = 0;
        LONGLONG ts = 0;
        ComPtr<IMFSample> sample;
        check(reader->ReadSample(audio, 0, nullptr, &flags, &ts, &sample), "read");
        if (flags & MF_SOURCE_READERF_ERROR) throw Fail{"the file is damaged"};
        if (flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED) throw Fail{"the sound format changes mid-file"};
        if (sample) {
            if (rs)
                push(rs.Get(), sample.Get(), to_encoder);
            else
                to_encoder(sample.Get());
        }
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) break;
    }
    if (rs) finish(rs.Get(), to_encoder);
    // The encoder drops its last, partial frame: a tenth of a second of silence carries the real end
    // of the recording (often someone's last words) through it.
    {
        DWORD bytes = out_rate / 10 * out_channels * 2;
        ComPtr<IMFSample> silence;
        ComPtr<IMFMediaBuffer> buf;
        check(MFCreateSample(&silence), "sample");
        check(MFCreateMemoryBuffer(bytes, &buf), "buffer");
        BYTE* data = nullptr;
        check(buf->Lock(&data, nullptr, nullptr), "lock");
        ZeroMemory(data, bytes);
        buf->Unlock();
        buf->SetCurrentLength(bytes);
        silence->AddBuffer(buf.Get());
        to_encoder(silence.Get());
    }
    finish(enc.Get(), to_file);
    if (writer.written() == 0) throw Fail{"nothing was encoded"};
}

}  // namespace

int convert_with_media_foundation(const wchar_t* in, HANDLE out, std::string* why) {
    bool com = SUCCEEDED(CoInitializeEx(nullptr, COINIT_MULTITHREADED));
    int code = 0;
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET))) {
        if (why) *why = "Media Foundation is not available (a Windows N edition without the Media Feature Pack?)";
        code = 3;
    } else {
        try {
            run(in, out);
        } catch (const Fail& f) {
            if (why) *why = f.why;
            code = 2;
        }
        MFShutdown();
    }
    if (com) CoUninitialize();
    return code;
}

bool media_foundation_present() {
    HMODULE m = LoadLibraryExW(L"mfplat.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!m) return false;
    FreeLibrary(m);
    return true;
}

}  // namespace clew::win
