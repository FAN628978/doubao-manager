from PIL import Image
img = Image.open(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png")
w, h = img.size
img.crop((0, 0, 360, 240)).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\side_top.png")
img.crop((0, 240, 360, 560)).save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\side_mid.png")
