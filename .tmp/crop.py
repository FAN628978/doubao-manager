from PIL import Image
img = Image.open(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png")
w, h = img.size
print(w, h)
# bottom composer strip (input area, last ~280px)
img.crop((0, h-300, w, h)).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_composer.png")
# middle suggestion cards area
img.crop((0, int(h*0.35), w, int(h*0.75))).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_mid.png")
# top bar
img.crop((0, 0, w, int(h*0.12))).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_top.png")
# sidebar
img.crop((0, 0, 320, h)).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_side.png")
