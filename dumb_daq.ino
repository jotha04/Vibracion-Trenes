#include <avr/io.h>
#include <avr/interrupt.h>
#include <avr/pgmspace.h>

// FASE 6 - Dumb DAQ Firmware
// ATmega328P @ 16 MHz
// 10 kHz Dual Channel (A0, A1)
// Baud rate: 1000000

#define SYNC_0 0xAA
#define SYNC_1 0x55
#define PROTOCOL_VERSION 0x01
#define SAMPLES_PER_BLOCK 32
#define PACKET_SIZE 138

// Ping-pong buffers embedded in packets
uint8_t packet_A[PACKET_SIZE];
uint8_t packet_B[PACKET_SIZE];

volatile uint8_t active_buffer = 0; // 0 for A, 1 for B
volatile uint8_t buffer_A_ready = 0;
volatile uint8_t buffer_B_ready = 0;
volatile uint8_t overflow_flag = 0;

volatile uint32_t sample_counter = 0;
volatile uint8_t sample_index_in_block = 0;

// CRC-16-CCITT Lookup Table
const uint16_t crc_table[256] PROGMEM = {
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6,
    0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485,
    0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4,
    0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
    0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823,
    0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
    0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12,
    0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
    0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41,
    0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
    0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70,
    0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
    0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F,
    0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E,
    0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D,
    0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C,
    0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB,
    0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
    0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A,
    0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
    0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9,
    0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
    0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8,
    0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0
};

uint16_t calculate_crc(const uint8_t *data, uint16_t length) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < length; i++) {
        uint8_t table_index = (crc >> 8) ^ data[i];
        crc = (crc << 8) ^ pgm_read_word(&crc_table[table_index]);
    }
    return crc;
}

void setup() {
    Serial.begin(1000000); // 1 Mbaud
    
    // Debug pin PORTB5 (D13) for ISR execution time measurement
    DDRB |= (1 << PORTB5);
    PORTB &= ~(1 << PORTB5);

    // Initialize static header fields
    packet_A[0] = SYNC_0;
    packet_A[1] = SYNC_1;
    packet_A[7] = SAMPLES_PER_BLOCK;

    packet_B[0] = SYNC_0;
    packet_B[1] = SYNC_1;
    packet_B[7] = SAMPLES_PER_BLOCK;

    // ADC Setup
    // REFS0: AVcc with external capacitor at AREF pin
    ADMUX = (1 << REFS0);
    // ADEN: ADC Enable
    // ADPS2: Prescaler 16 (16MHz / 16 = 1MHz -> fast sampling, ~8.0-8.5 ENOB)
    ADCSRA = (1 << ADEN) | (1 << ADPS2);

    // Timer1 Setup for 10kHz Interrupts
    cli();
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1  = 0;
    OCR1A = 1599; // (16,000,000 / (1 * 10,000)) - 1 = 1599
    TCCR1B |= (1 << WGM12); // CTC mode
    TCCR1B |= (1 << CS10);  // Prescaler 1
    TIMSK1 |= (1 << OCIE1A); // Enable compare interrupt
    sei();
}

ISR(TIMER1_COMPA_vect) {
    // Start of ISR - raise debug pin
    PORTB |= (1 << PORTB5);

    // Read A0 (CH1)
    ADMUX = (1 << REFS0) | 0; // Select A0
    ADCSRA |= (1 << ADSC);    // Start conversion
    while (ADCSRA & (1 << ADSC)); // Wait for conversion complete
    uint16_t val_ch1 = ADC;

    // Read A1 (CH2)
    ADMUX = (1 << REFS0) | 1; // Select A1
    ADCSRA |= (1 << ADSC);    // Start conversion
    while (ADCSRA & (1 << ADSC)); // Wait for conversion complete
    uint16_t val_ch2 = ADC;

    // Reset ADMUX to A0 early to allow setting up next conversion
    ADMUX = (1 << REFS0) | 0;

    // Direct storage in payload bytes
    uint8_t payload_idx = 8 + (sample_index_in_block * 4);
    
    if (active_buffer == 0) {
        packet_A[payload_idx] = val_ch1 & 0xFF;
        packet_A[payload_idx + 1] = (val_ch1 >> 8) & 0xFF;
        packet_A[payload_idx + 2] = val_ch2 & 0xFF;
        packet_A[payload_idx + 3] = (val_ch2 >> 8) & 0xFF;
    } else {
        packet_B[payload_idx] = val_ch1 & 0xFF;
        packet_B[payload_idx + 1] = (val_ch1 >> 8) & 0xFF;
        packet_B[payload_idx + 2] = val_ch2 & 0xFF;
        packet_B[payload_idx + 3] = (val_ch2 >> 8) & 0xFF;
    }

    sample_index_in_block++;
    sample_counter++;

    // Buffer complete?
    if (sample_index_in_block >= SAMPLES_PER_BLOCK) {
        sample_index_in_block = 0;
        uint32_t base_index = sample_counter - SAMPLES_PER_BLOCK;

        if (active_buffer == 0) {
            if (buffer_A_ready) overflow_flag = 1;
            
            packet_A[2] = PROTOCOL_VERSION | (overflow_flag ? 0x80 : 0x00);
            packet_A[3] = base_index & 0xFF;
            packet_A[4] = (base_index >> 8) & 0xFF;
            packet_A[5] = (base_index >> 16) & 0xFF;
            packet_A[6] = (base_index >> 24) & 0xFF;
            
            overflow_flag = 0;
            buffer_A_ready = 1;
            active_buffer = 1;
        } else {
            if (buffer_B_ready) overflow_flag = 1;
            
            packet_B[2] = PROTOCOL_VERSION | (overflow_flag ? 0x80 : 0x00);
            packet_B[3] = base_index & 0xFF;
            packet_B[4] = (base_index >> 8) & 0xFF;
            packet_B[5] = (base_index >> 16) & 0xFF;
            packet_B[6] = (base_index >> 24) & 0xFF;
            
            overflow_flag = 0;
            buffer_B_ready = 1;
            active_buffer = 0;
        }
    }

    // End of ISR - lower debug pin
    PORTB &= ~(1 << PORTB5);
}

void loop() {
    if (buffer_A_ready) {
        uint16_t crc = calculate_crc(&packet_A[2], 134);
        packet_A[136] = crc & 0xFF;
        packet_A[137] = (crc >> 8) & 0xFF;
        
        Serial.write(packet_A, PACKET_SIZE);
        buffer_A_ready = 0;
    }
    
    if (buffer_B_ready) {
        uint16_t crc = calculate_crc(&packet_B[2], 134);
        packet_B[136] = crc & 0xFF;
        packet_B[137] = (crc >> 8) & 0xFF;
        
        Serial.write(packet_B, PACKET_SIZE);
        buffer_B_ready = 0;
    }
}
