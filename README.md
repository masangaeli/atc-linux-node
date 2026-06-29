# General Automation Structure

1. Start Screen
==================================
docker exec -d atc_linux_node_001 sh -c 'Xvfb :1 -screen 0 1280x604x16'

docker exec -d atc_linux_node_001 sh -c 'Xvfb :2 -screen 0 1280x604x16'



2. Start MT5
==================================
python3 scripts/start_mt5.py



3. Initiate ATC MT5 Config
==================================
python3 scripts/initiate_mt5_config.py

- Copy Config to right dirs
- Perform MT5 Broker Search
- Perform MT5 Account Login
- Stay in Tade Panel ready to receive trade commands



4. Start MT5 ATC Process
==================================
python3 scripts/initiate_atc_processor.py