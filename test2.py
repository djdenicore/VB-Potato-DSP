import sounddevice as sd

print("--- Все доступные аудиоустройства ---")
for i, dev in enumerate(sd.query_devices()):
    print(f"[{i}] {dev['name']} (Входов: {dev['max_input_channels']}, Выходов: {dev['max_output_channels']})")