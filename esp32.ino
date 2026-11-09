#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "soc/gpio_reg.h"

// ---------- Access point settings ----------
const char* ACCESS_POINT_SSID     = "miniMazinga-WiFi";
const char* ACCESS_POINT_PASSWORD = "daq12345";
const int   ACCESS_POINT_CHANNEL  = 6;
const int   ACCESS_POINT_MAX_CLIENTS = 2;

const IPAddress ACCESS_POINT_IP(192, 168, 4, 1);
const IPAddress ACCESS_POINT_SUBNET_MASK(255, 255, 255, 0);

// ---------- UDP streaming ----------
const uint16_t UDP_DESTINATION_PORT = 5000;
const IPAddress UDP_DESTINATION_IP(192, 168, 4, 255);   // broadcast; or the PC's IP, e.g. 192.168.4.2

WiFiUDP udpSocket;
uint32_t nextPacketSequenceNumber = 0;

// ---------- Input pins ----------
const uint8_t NUMBER_OF_INPUT_PINS = 10;
const uint8_t INPUT_GPIO_NUMBERS[NUMBER_OF_INPUT_PINS] = {16, 17, 18, 19, 21, 22, 23, 25, 26, 27};
uint32_t inputPinsBitmask = 0;          // one bit set per monitored GPIO (GPIO 0-31)

// ---------- Event queue (polling loop -> network task) ----------
struct InputEvent 
{
  uint32_t timestampMicroseconds;       // micros() when the change was detected
  uint32_t inputPinsState;              // masked GPIO_IN_REG value at that moment
};

const uint16_t EVENT_QUEUE_LENGTH = 256;
QueueHandle_t inputEventQueue;

// Converts the masked register value to a string such as "0100000010"
void formatPinsStateAsText(uint32_t inputPinsState, char* outputText) 
{
  for (uint8_t pinIndex = 0; pinIndex < NUMBER_OF_INPUT_PINS; pinIndex++) 
  {
    bool pinIsHigh = (inputPinsState >> INPUT_GPIO_NUMBERS[pinIndex]) & 1;
    outputText[pinIndex] = pinIsHigh ? '1' : '0';
  }
  outputText[NUMBER_OF_INPUT_PINS] = '\0';
}

// Network task on core 0: takes events from the queue and sends them over UDP
void networkTask(void*) 
{
  char pinsStateText[NUMBER_OF_INPUT_PINS + 1];
  char packetText[64];
  InputEvent receivedEvent;

  for (;;) // Infinite loop
  {
    if (xQueueReceive(inputEventQueue, &receivedEvent, portMAX_DELAY) == pdTRUE) 
    {
      formatPinsStateAsText(receivedEvent.inputPinsState, pinsStateText);
      snprintf(packetText, sizeof(packetText), "E %lu %lu %s\n",
               (unsigned long)nextPacketSequenceNumber++,
               (unsigned long)receivedEvent.timestampMicroseconds,
               pinsStateText);

      udpSocket.beginPacket(UDP_DESTINATION_IP, UDP_DESTINATION_PORT);
      udpSocket.print(packetText);
      udpSocket.endPacket();
    }
  }
}

void setup() 
{
  Serial.begin(115200);

  for (uint8_t pinIndex = 0; pinIndex < NUMBER_OF_INPUT_PINS; pinIndex++) 
  {
    pinMode(INPUT_GPIO_NUMBERS[pinIndex], INPUT_PULLDOWN);
    inputPinsBitmask |= (1UL << INPUT_GPIO_NUMBERS[pinIndex]);
  }

  inputEventQueue = xQueueCreate(EVENT_QUEUE_LENGTH, sizeof(InputEvent));

  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(ACCESS_POINT_IP, ACCESS_POINT_IP, ACCESS_POINT_SUBNET_MASK);
  WiFi.softAP(ACCESS_POINT_SSID, ACCESS_POINT_PASSWORD,
              ACCESS_POINT_CHANNEL, 0 /*not hidden*/, ACCESS_POINT_MAX_CLIENTS);
  WiFi.setSleep(false);

  Serial.print("Access point up, IP: ");
  Serial.println(WiFi.softAPIP());

  xTaskCreatePinnedToCore(networkTask, "network", 4096, nullptr, 1, nullptr, 0);
}

// Polling on core 1: one register read covers all 10 pins
void loop() 
{
  static uint32_t previousInputPinsState = 0;

  uint32_t currentInputPinsState = REG_READ(GPIO_IN_REG) & inputPinsBitmask;

  if (currentInputPinsState != previousInputPinsState) 
  {
    previousInputPinsState = currentInputPinsState;

    bool atLeastOneInputIsHigh = (currentInputPinsState != 0);
    if (atLeastOneInputIsHigh) 
    {
      InputEvent newEvent = { micros(), currentInputPinsState };
      xQueueSend(inputEventQueue, &newEvent, 0);   // never blocks the polling
    }
  }
}