#  Kiwi

A swift app that runs on a raspberry pi as a plugin host

Developed on a Mac

## Development setup

How to setup development environment

Inspo: https://medium.com/@programmingpassion/cross-compile-swift-code-for-raspberry-pi-1737da0301da

### MAC:

```sh
brew install coreutils jq wget
```

Not in the project directory:
```
git clone https://github.com/CSCIX65G/SwiftCrossCompilers.git
cd SwiftCrossCompilers
```

### PI:

Install swift: swift-arm.com

```
curl -s https://archive.swiftlang.xyz/install.sh | sudo bash
sudo apt install swiftlang
```

## Building:

### On Mac:

```
cd {project dir}
swift build --destination /Library/Developer/Destinations/arm64-5.9.1-RELEASE.json
```

Copy build:
```
scp -r .build jonasbarsten@192.168.0.28:/home/jonasbarsten/swift_kiwi/
```

### On PI:

```
cd /home/jonasbarsten/swift_kiwi
.build/debug/swift_kiwi
```
