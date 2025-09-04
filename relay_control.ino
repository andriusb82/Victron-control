/*
  Victron MultiPlus Remote (ON/CH) relay controller
  - Nano + 2-ch relay (assumed ACTIVE-LOW: LOW=energized, opens NC)
  - D2 controls "ON" link, D3 controls "CH" link
  - Default at boot: both enabled (NC closed) -> relays OFF (pins HIGH)
  - Serial protocol (115200 baud):
      ON 1 | ON 0
      CH 1 | CH 0
      ALL 1 | ALL 0
      STATE?
      HELP
  - Replies: OK / ERR <reason> / STATE ON=<0|1> CH=<0|1>
*/

const int RELAY_ON_PIN = 2;   // controls Victron ON (NC between "+" and "ON")
const int RELAY_CH_PIN = 3;   // controls Victron CH (NC between "+" and "CH")

// Set this to false if your relay board is active-HIGH (HIGH=energized)
const bool RELAY_ACTIVE_LOW = true;

// Internal state: "enabled" means Victron link closed (NC closed)
bool inverterEnabled = true;  // ON link
bool chargerEnabled  = true;  // CH link

void applyRelay(int pin, bool enabled) {
  // enabled=true  -> keep NC CLOSED -> relay DE-energized
  // enabled=false -> open NC        -> relay energized
  bool energize = !enabled;
  int level;
  if (RELAY_ACTIVE_LOW) {
    level = energize ? LOW : HIGH;
  } else {
    level = energize ? HIGH : LOW;
  }
  digitalWrite(pin, level);
}

void publishState() {
  Serial.print(F("STATE ON="));
  Serial.print(inverterEnabled ? 1 : 0);
  Serial.print(F(" CH="));
  Serial.println(chargerEnabled ? 1 : 0);
}

void setInverter(bool enabled) {
  inverterEnabled = enabled;
  applyRelay(RELAY_ON_PIN, inverterEnabled);
}

void setCharger(bool enabled) {
  chargerEnabled = enabled;
  applyRelay(RELAY_CH_PIN, chargerEnabled);
}

String trimLower(const String &s) {
  String t = s;
  t.trim();
  t.toLowerCase();
  return t;
}

void setup() {
  pinMode(RELAY_ON_PIN, OUTPUT);
  pinMode(RELAY_CH_PIN, OUTPUT);

  // Fail-safe defaults: both enabled -> relays OFF (NC closed)
  if (RELAY_ACTIVE_LOW) {
    digitalWrite(RELAY_ON_PIN, HIGH);
    digitalWrite(RELAY_CH_PIN, HIGH);
  } else {
    digitalWrite(RELAY_ON_PIN, LOW);
    digitalWrite(RELAY_CH_PIN, LOW);
  }

  Serial.begin(115200);
  Serial.setTimeout(50);

  // Apply initial logical state to match pins
  setInverter(true);
  setCharger(true);

  Serial.println(F("Victron Remote Relay Ready"));
  publishState();
  Serial.println(F("Type HELP for commands"));
}

bool parseBool(const String &val, bool &out) {
  String v = trimLower(val);
  if (v == "1" || v == "on" || v == "true" || v == "enable" || v == "enabled") { out = true;  return true; }
  if (v == "0" || v == "off"|| v == "false"|| v == "disable"|| v == "disabled"){ out = false; return true; }
  return false;
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    // Split into tokens
    String cmd = line;
    int sp = line.indexOf(' ');
    String arg = "";
    if (sp >= 0) {
      cmd = line.substring(0, sp);
      arg = line.substring(sp + 1);
    }
    cmd.toLowerCase();
    arg.trim();

    if (cmd == "help") {
      Serial.println(F("Commands:"));
      Serial.println(F("  ON 1 | ON 0"));
      Serial.println(F("  CH 1 | CH 0"));
      Serial.println(F("  ALL 1 | ALL 0"));
      Serial.println(F("  STATE?"));
      Serial.println(F("  HELP"));
      return;
    }

    if (cmd == "state?") {
      publishState();
      return;
    }

    if (cmd == "on") {
      bool v;
      if (!parseBool(arg, v)) { Serial.println(F("ERR bad ON value")); return; }
      setInverter(v);
      Serial.println(F("OK"));
      publishState();
      return;
    }

    if (cmd == "ch") {
      bool v;
      if (!parseBool(arg, v)) { Serial.println(F("ERR bad CH value")); return; }
      setCharger(v);
      Serial.println(F("OK"));
      publishState();
      return;
    }

    if (cmd == "all") {
      bool v;
      if (!parseBool(arg, v)) { Serial.println(F("ERR bad ALL value")); return; }
      setInverter(v);
      setCharger(v);
      Serial.println(F("OK"));
      publishState();
      return;
    }

    Serial.println(F("ERR unknown command (type HELP)"));
  }
}
