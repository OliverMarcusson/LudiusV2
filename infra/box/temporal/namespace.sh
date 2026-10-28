a="--address 127.0.0.1:7233"
temporal operator cluster health $a
temporal operator namespace describe -n default $a >/dev/null 2>&1 || temporal operator namespace create -n default $a
