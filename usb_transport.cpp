// Reviewable user-space IOKit USB transport. Compiling does not access hardware.
// No constructor, main(), discovery advertisement, reset, seize or installation.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <new>
#include <vector>

struct MijiaUSB {
    IOUSBInterfaceInterface942 **interface = nullptr;
    UInt8 in_pipe = 0;
    UInt8 out_pipe = 0;
};

static void error(char *dest, size_t cap, const char *operation, IOReturn result) {
    if (dest && cap) std::snprintf(dest, cap, "%s: 0x%08x", operation, unsigned(result));
}

static int property_number(io_service_t service, CFStringRef name) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(service, name, kCFAllocatorDefault, 0);
    int result = -1;
    if (value && CFGetTypeID(value) == CFNumberGetTypeID())
        CFNumberGetValue(static_cast<CFNumberRef>(value), kCFNumberIntType, &result);
    if (value) CFRelease(value);
    return result;
}

static bool matches_serial(io_service_t service, const char *serial) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(service, CFSTR("USB Serial Number"), kCFAllocatorDefault, 0);
    char text[256] = {};
    bool ok = value && CFGetTypeID(value) == CFStringGetTypeID() &&
        CFStringGetCString(static_cast<CFStringRef>(value), text, sizeof(text), kCFStringEncodingUTF8) &&
        std::strcmp(text, serial) == 0;
    if (value) CFRelease(value);
    return ok;
}

extern "C" void mijia_usb_close(void *handle) {
    auto *usb = static_cast<MijiaUSB *>(handle);
    if (!usb) return;
    if (usb->interface) {
        (*usb->interface)->USBInterfaceClose(usb->interface);
        (*usb->interface)->Release(usb->interface);
    }
    delete usb;
}

extern "C" void *mijia_usb_open(const char *serial, char *err, size_t cap) {
    if (!serial || !*serial) { error(err, cap, "explicit serial required", kIOReturnBadArgument); return nullptr; }
    io_iterator_t iterator = IO_OBJECT_NULL;
    IOReturn result = IOServiceGetMatchingServices(kIOMainPortDefault,
        IOServiceMatching("IOUSBHostInterface"), &iterator);
    if (result != kIOReturnSuccess) { error(err, cap, "enumerate interfaces", result); return nullptr; }
    std::vector<io_service_t> matches;
    io_service_t service;
    while ((service = IOIteratorNext(iterator))) {
        if (property_number(service, CFSTR("idVendor")) == 0x302c &&
            property_number(service, CFSTR("idProduct")) == 0x3008 &&
            property_number(service, CFSTR("bInterfaceNumber")) == 2 &&
            property_number(service, CFSTR("bInterfaceClass")) == 255 &&
            property_number(service, CFSTR("bInterfaceSubClass")) == 255 &&
            property_number(service, CFSTR("bInterfaceProtocol")) == 255 &&
            matches_serial(service, serial)) matches.push_back(service);
        else IOObjectRelease(service);
    }
    IOObjectRelease(iterator);
    if (matches.size() != 1) {
        for (auto object : matches) IOObjectRelease(object);
        error(err, cap, "expected one matching interface 2", kIOReturnNotFound);
        return nullptr;
    }
    IOCFPlugInInterface **plugin = nullptr;
    SInt32 score = 0;
    result = IOCreatePlugInInterfaceForService(matches[0], kIOUSBInterfaceUserClientTypeID,
                                               kIOCFPlugInInterfaceID, &plugin, &score);
    IOObjectRelease(matches[0]);
    if (result != kIOReturnSuccess || !plugin) { error(err, cap, "create interface plugin", result); return nullptr; }
    auto *usb = new (std::nothrow) MijiaUSB;
    if (!usb) { (*plugin)->Release(plugin); error(err, cap, "allocate", kIOReturnNoMemory); return nullptr; }
    HRESULT query = (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID942),
                                               reinterpret_cast<LPVOID *>(&usb->interface));
    (*plugin)->Release(plugin);
    if (query || !usb->interface) { delete usb; error(err, cap, "query USB interface", kIOReturnUnsupported); return nullptr; }
    UInt8 number = 0xff;
    result = (*usb->interface)->GetInterfaceNumber(usb->interface, &number);
    if (result != kIOReturnSuccess || number != 2) {
        (*usb->interface)->Release(usb->interface); delete usb;
        error(err, cap, "verify interface number", kIOReturnBadArgument); return nullptr;
    }
    result = (*usb->interface)->USBInterfaceOpen(usb->interface);
    if (result != kIOReturnSuccess) {
        (*usb->interface)->Release(usb->interface); delete usb;
        error(err, cap, "open interface (no seize)", result); return nullptr;
    }
    UInt8 endpoints = 0;
    result = (*usb->interface)->GetNumEndpoints(usb->interface, &endpoints);
    unsigned ins = 0, outs = 0;
    for (UInt8 pipe = 1; result == kIOReturnSuccess && pipe <= endpoints; ++pipe) {
        UInt8 direction, endpoint, type, interval;
        UInt16 packet;
        result = (*usb->interface)->GetPipeProperties(usb->interface, pipe, &direction, &endpoint,
                                                     &type, &packet, &interval);
        if (result != kIOReturnSuccess) break;
        if (type != kUSBBulk) continue;
        if (direction == kUSBIn) { usb->in_pipe = pipe; ++ins; }
        if (direction == kUSBOut) { usb->out_pipe = pipe; ++outs; }
    }
    if (result != kIOReturnSuccess || ins != 1 || outs != 1) {
        mijia_usb_close(usb); error(err, cap, "expected one bulk IN and OUT", kIOReturnUnsupported); return nullptr;
    }
    return usb;
}

extern "C" int64_t mijia_usb_write(void *handle, const void *data, uint32_t count,
                                   uint32_t timeout, char *err, size_t cap) {
    auto *usb = static_cast<MijiaUSB *>(handle);
    if (!usb || !data || !count || !timeout || timeout > 60000) {
        error(err, cap, "write arguments", kIOReturnBadArgument); return -1;
    }
    IOReturn result = (*usb->interface)->WritePipeTO(usb->interface, usb->out_pipe,
        const_cast<void *>(data), count, timeout, timeout);
    if (result != kIOReturnSuccess) { error(err, cap, "bulk write (outcome uncertain)", result); return -1; }
    return count;
}

extern "C" int64_t mijia_usb_read(void *handle, void *data, uint32_t capacity,
                                  uint32_t timeout, char *err, size_t cap) {
    auto *usb = static_cast<MijiaUSB *>(handle);
    if (!usb || !data || !capacity || !timeout || timeout > 60000) {
        error(err, cap, "read arguments", kIOReturnBadArgument); return -1;
    }
    UInt32 count = capacity;
    IOReturn result = (*usb->interface)->ReadPipeTO(usb->interface, usb->in_pipe,
                                                  data, &count, timeout, timeout);
    if (result != kIOReturnSuccess) { error(err, cap, "bulk read", result); return -1; }
    return count;
}
