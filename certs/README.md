# Сертификаты НУЦ Минцифры

GigaChat использует TLS-сертификаты, выпущенные НУЦ Минцифры. Без них соединение с
`api.giga.chat` и OAuth-сервером Сбера не устанавливается:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain
```

Файлы в этой папке взяты с официального портала <https://www.gosuslugi.ru/crt>
(прямые ссылки: `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`,
`https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt`).

`Dockerfile` копирует их в системное хранилище контейнера и дополнительно добавляет в `certifi`,
поэтому в облаке (Amvera) ничего настраивать не нужно.

Если сертификаты обновятся — скачайте новые файлы с портала Госуслуг и замените их здесь.
Для запуска на своём компьютере без установки в систему укажите путь к корневому сертификату:
`GIGACHAT_CA_BUNDLE=certs/russian_trusted_root_ca.crt` (приложение само соберёт общий bundle).
