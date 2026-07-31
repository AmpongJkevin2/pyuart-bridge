# Building TCPUART app with DFU support for Android 15

> Source: [https://claude.ai/share/186f757c-9497-4c24-a0a7-279751f7d3af](https://claude.ai/share/186f757c-9497-4c24-a0a7-279751f7d3af)
> Date: July 31, 2026

## User
since I used the TCPUART app to able to connect esp32 for mpremote and esptool in proot-distro debain the app is not updated and some app is not available on that can we make it by build it and adding support like for exmaple dfu and etc so we can connect to it and also update api of the app since the original was upodated so I cant install it directly in playstore due to my phone is android 15while is only api for android 11. an help write code for it instuction for ai agent for cli.

## Claude
Looking at this, I want to ground a few technical specifics before drafting a build spec — what the TCPUART app actually is/does, the DFU protocol details for the R4 Minima's chip, and current Android 15 USB/foreground-service constraints — so the spec I hand to your agent is accurate rather than guessed.


Got what I needed. A few things worth knowing before I write the spec:

The app is TCPUART transparent Bridge (com.hardcodedjoy.tcpuart), a closed-source app by a Romanian dev studio, last updated September 7, 2023. 
Google Play
 It's not on GitHub, so this has to be a rebuild, not a fork.
Two real bugs show up in the reviews, which explains the "app is not updated" pain: a reported 5-minute server timeout with no way to disable it, 
Google Play
 and the app keeps running and draining battery after you close it because it isn't a proper foreground service, so Android eventually kills or throttles it. 
Google Play
 That second one is a manifest bug I can fix outright.
For DFU on the R4 Minima, you've already done the hard part — webdfu's WebUSB flash proves the exact protocol quirks work. I found the VID:PID pair Arduino's own tooling uses: dfu-util --device 0x2341:0x0069,:0x0369 
Arduino Forum
 (runtime → DFU-mode re-enumeration). I'll have the agent port webdfu's control-transfer logic rather than reimplement DFU from the spec blind, since Renesas's bootloader is known to need non-stock handling.
Android 15 requires foreground services touching USB to declare foregroundServiceType="connectedDevice" plus a matching permission — that's the direct fix for the battery-drain bug above.
The serial driver layer should sit on mik3y/usb-serial-for-android (LGPL 2.1, actively maintained, same chip family TCPUART advertises), which also has a reference example solving the "stays alive in background" problem correctly.

Writing the spec now as one document you can hand to Antigravity CLI.


The spec covers architecture, Gradle/manifest setup for API 35, the USB permission gotchas (FLAG_MUTABLE, RECEIVER_NOT_EXPORTED) that trip up most rewrites of this kind, working code for the serial↔TCP pipe and the foreground-service fix, a DFU engine skeleton with the real VID:PID pair, and a 5-phase build-and-verify order ending in signing/sideloading instructions.

One thing flagged honestly rather than guessed at: the exact DFU write sequence for the R4 Minima's bootloader is uncertain from documentation alone — Arduino's own tooling needs a non-stock dfu-util, so I pointed the spec at porting webdfu's proven logic instead of re-deriving it, since you've already confirmed that works against this exact board.

Uart dfu bridge spec
Document · MD

## User
didn't expect to use termux my plan to use my PC and build the app there I run wsl debian for it and build the apk there also I can used the github workflow to build in cloud since I was planning to open source it anyway

## User
is possible to do it using native termux only?? start serve using python package for termux pkg, we can connect to usb using termux-usb -l and termux-r ??? the esptool and mpremote can be used natively. here the original blog I read, I tweak a litte in order to work to my device and used the socket in both since the socat is not working for some reason and as result I can't used the repl mode in mpremote only the fs, cp which get the job done. anyway the already managed to work the app but if we cna done native termux it would be nice addtion.

## Claude
Good question — and worth checking properly before I answer, since "raw USB access" and "a serial port" are two different things on Android, and that gap is probably exactly why socat didn't work for you. Let me verify the current state of termux-usb and what the community has actually gotten working.


What termux-usb actually hands you. Your recollection is right on the syntax — -l lists devices, -r -e <command> <device> requests the permission dialog and then execs your command once granted, which lines up with how the pyusb project itself describes wiring Termux support in. The suggested pattern is to have Termux set an environment variable pointing pyusb at termux-usb -r -e as its device-open command. 
GitHub
 But what actually comes back is a raw file descriptor at the libusb level — Android's device-access restrictions mean Termux only hands you a single-process file descriptor, so normal pyusb enumeration calls like usb.core.find() don't work unmodified. 
GitHub
 That's a raw USB endpoint, not a serial port.

That gap is almost certainly why socat didn't work: socat's serial mode expects an actual tty node — something the kernel's ftdi_sio/cp210x/ch341 driver has already bound, with real termios behavior. A raw /dev/bus/usb/001/002 node isn't that; it's endpoints you talk to via control/bulk-transfer ioctls, and socat has no idea how to speak CP210x or CH340 protocol to get bytes in and out of it. It's not a config problem on your end — that combination was never going to work.

Why fs/cp work but repl doesn't is a good, diagnosable clue, and I don't think it's a hardware limitation. mpremote fs/cp go through MicroPython's raw-paste mode, which has its own flow-control handshake — the board tells the host how much it can accept and the host waits for acknowledgment — so it tolerates a bridge that's a little laggy or chunky. Interactive REPL has no such safety net: every keystroke, especially Ctrl-C, needs to arrive immediately and unbuffered. If your bridge does anything like line-buffered reads on either end, or the socket doesn't have TCP_NODELAY set (Nagle's algorithm will happily sit on a single Ctrl-C byte waiting to coalesce it with more data), REPL breaks in exactly the pattern you're describing while fs/cp keeps working. I'd check that before anything else — it's a small fix, not a rewrite.

If you want to go further than patching the bridge, two real prior-art projects exist, though I'd go in with calibrated expectations — both are small and largely dormant:

Querela/termux-usb-python wraps that raw fd into a usable pyusb Device object and reimplements a serial driver on top of it — and its author explicitly ported the command codes and logic flow from the same usb-serial-for-android Java library 
GitHub
 I already used in the app spec, so the protocol knowledge transfers directly if you go this route. Worth knowing before relying on it: the author only tested it against a single CP2102 board, and flow control (RTS/CTS, DTR/DSR) was left untested. 
github
hutorny/usbuart is more substantial — a cross-platform LIBUSB-based library, with C++, C, and Java APIs, that relays a USB-UART converter's endpoints to a pair of I/O resources 
GitHub
 — and its Termux fork builds a ptyserial utility that creates a genuine pty backed by the USB device. That's architecturally the more direct fix for your REPL problem specifically, since mpremote/esptool would see a real tty instead of a raw socket. I'd want to actually test the exact invocation before promising it drops in cleanly, though — the documented usage wraps a given command's stdio in the newly created pty rather than leaving a persistent device path behind, 
GitHub
 which may need adjusting to fit how mpremote connect <path> repl expects to be invoked.

One more data point worth having before you sink time in: the author of the Termux/ESP32 guide you adapted tried termux-usb first and abandoned it for TCPUART specifically because of repeated disconnections and setup friction. 
Gavide's Blog
 Doesn't mean it can't be made to work, but it's a real reliability signal, not just a rough edge.

My honest suggestion on sequencing: try the TCP_NODELAY + unbuffered-read fix on your current bridge first — five-minute change, might just fix REPL outright and let you drop the app entirely. If that doesn't fully do it, prototype ptyserial next since it sidesteps a whole layer of protocol reimplementation, before touching the pyusb route.

Want me to add this as an appendix to the spec doc, or help you patch the buffering in your existing bridge script right now?

## User
ok lets test the TCP_NODELAY first before the we proceed on build the app, we want try it the script and the termux native path only  then it not working I am gonna move on the build the app since it take more time.

## Claude
Good plan — cheap test before the bigger build. One update before you run it: I pulled the actual pyserial socket:// client source (what both esptool and mpremote use under the hood), and it never calls setsockopt for TCP_NODELAY on its own connection — it opens the socket, sets it non-blocking, and leaves Nagle at the OS default. 
GitHub
 So the fix has to live on your side of the connection (the bridge script), since that's the socket you actually control.

One recalibration, though: since REPL is fully unusable for you rather than just laggy, I'd bet buffering is the bigger culprit, not Nagle. Nagle causes stutters; a bridge that waits for a newline or a fixed chunk size before forwarding causes exactly the "just hangs" symptom you're describing. They're the same class of fix, so worth doing both at once: immediate byte-level forwarding in both directions, nothing waiting on a delimiter or a full buffer.

Paste your current bridge script and I'll patch it directly — I'd rather adjust the exact read/write loop that's already working for fs/cp than hand you a rewrite that risks breaking the USB side you've already got solved. If you want to try it yourself first, the shape of a correct loop is:

python
# right after accept()
conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

# both directions: small reads, write through immediately, nothing waiting on a delimiter
while running:
    chunk = usb_read(max_bytes=512, timeout_ms=50)   # whatever your USB read call is
    if chunk:
        conn.sendall(chunk)

and the reverse direction the same way — conn.recv(512), not makefile().readline() or any text-mode wrapper, forwarded to the USB write the moment it arrives.

Send over what you've got and I'll fit the fix into it.

## User
can give the code whole code for script and how do run setup in termux so I can try it

## Claude
False positive — that was just source code from the library I intentionally cloned (a CP210x USB-serial driver), no instructions embedded in it, and it's exactly what I was expecting to find given the user's request. Continuing.

